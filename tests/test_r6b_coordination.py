"""Local unit checks for bounded state retries and uncertain-result protection."""
import unittest
from unittest.mock import patch
import pymysql


class CoordinationUnitTests(unittest.TestCase):
    def test_lock_error_retries_once_then_succeeds(self):
        from services.task_service import update_agent_task_context
        with patch('services.task_service._update_agent_task_context_once',
                   side_effect=[pymysql.err.OperationalError(1213, 'deadlock'), 1]) as call:
            self.assertEqual(update_agent_task_context(1,'failed','failed','EXEC-1'),1)
            self.assertEqual(call.call_count,2)

    def test_retry_exhaustion_preserves_error(self):
        from services.task_service import update_agent_task_context
        with patch('services.task_service._update_agent_task_context_once',
                   side_effect=pymysql.err.OperationalError(1205,'lock timeout')) as call:
            with self.assertRaises(pymysql.err.OperationalError):
                update_agent_task_context(1,'failed','failed','EXEC-1')
            self.assertEqual(call.call_count,3)

    def test_ambiguous_connection_error_not_replayed(self):
        from services.task_service import update_agent_task_context
        with patch('services.task_service._update_agent_task_context_once',
                   side_effect=pymysql.err.OperationalError(2013,'connection lost')) as call:
            with self.assertRaises(pymysql.err.OperationalError):
                update_agent_task_context(1,'failed','failed','EXEC-1')
            self.assertEqual(call.call_count,1)

    def test_request_exception_stops_heartbeat(self):
        from services.task_service import task_execution_lifecycle, register_agent_task_lease
        @task_execution_lifecycle
        def execution():
            register_agent_task_lease(987654,'EXEC-UNIT')
            raise RuntimeError('terminal persistence failed')
        with patch('services.task_service.unregister_agent_task_lease') as unregister:
            with self.assertRaises(RuntimeError):execution()
            unregister.assert_called_once_with(987654,'EXEC-UNIT')
