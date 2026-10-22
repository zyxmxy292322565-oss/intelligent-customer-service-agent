"""Deterministic scheduler tests: no real network or database access."""
from collections import OrderedDict
import threading
import unittest
from unittest.mock import patch

from services import task_service as tasks


class HeartbeatFairnessTests(unittest.TestCase):
    def setUp(self):
        self.registry = patch.object(tasks, '_lease_registry', OrderedDict())
        self.registry.start()
        self.addCleanup(self.registry.stop)
        for task_id in range(1, 141):
            tasks.register_agent_task_lease(task_id, f'owner-{task_id}')

    def test_140_leases_four_rounds_record_exact_rotation(self):
        rounds = []
        for _ in range(4):
            seen = []
            with patch.object(tasks, 'renew_agent_task_lease',
                              side_effect=lambda task, owner: seen.append(task) or True):
                self.assertEqual(tasks.renew_registered_task_leases(100),
                                 {'renewed': 100, 'lost': 0, 'errors': 0})
            rounds.append(seen)
        self.assertEqual(rounds[0], list(range(1, 101)))
        self.assertEqual(rounds[1], list(range(101, 141)) + list(range(1, 61)))
        self.assertEqual(rounds[2], list(range(61, 141)) + list(range(1, 21)))
        self.assertEqual(rounds[3], list(range(21, 121)))
        self.assertEqual(set(rounds[0] + rounds[1]), set(range(1, 141)))
        print('HEARTBEAT_ROUNDS', rounds)

    def test_concurrent_schedulers_reserve_fair_batches_before_io(self):
        seen, errors = [], []
        gate = threading.Barrier(2)
        def run():
            try:
                gate.wait()
                tasks.renew_registered_task_leases(100)
            except Exception as exc:
                errors.append(exc)
        with patch.object(tasks, 'renew_agent_task_lease',
                          side_effect=lambda task, owner: seen.append(task) or True):
            workers = [threading.Thread(target=run) for _ in range(2)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(5)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertFalse(errors)
        self.assertEqual(len(seen), 200)
        self.assertEqual(set(seen), set(range(1, 141)))

    def test_errors_rotate_and_lost_owner_is_removed(self):
        def renew(task, owner):
            if task == 1: raise RuntimeError('injected database failure')
            return task != 2
        with patch.object(tasks, 'renew_agent_task_lease', side_effect=renew):
            result = tasks.renew_registered_task_leases(100)
        self.assertEqual(result, {'renewed': 98, 'lost': 1, 'errors': 1})
        seen = []
        with patch.object(tasks, 'renew_agent_task_lease',
                          side_effect=lambda task, owner: seen.append(task) or True):
            tasks.renew_registered_task_leases(100)
        self.assertEqual(seen[:40], list(range(101, 141)))
        self.assertNotIn((2, 'owner-2'), tasks._lease_registry)

    def test_repeat_registration_does_not_jump_queue(self):
        with patch.object(tasks, 'renew_agent_task_lease', return_value=True):
            tasks.renew_registered_task_leases(100)
        tasks.register_agent_task_lease(101, 'owner-101')
        self.assertEqual(next(iter(tasks._lease_registry)), (101, 'owner-101'))

    def test_process_restart_does_not_resurrect_old_requests(self):
        # Only a live request registers. Restarting the scheduler process
        # cannot reconstruct abandoned requests and extend their owners.
        with patch.object(tasks, '_lease_registry', OrderedDict()):
            with patch.object(tasks, 'renew_agent_task_lease') as renew:
                self.assertEqual(tasks.renew_registered_task_leases(),
                                 {'renewed': 0, 'lost': 0, 'errors': 0})
                renew.assert_not_called()
