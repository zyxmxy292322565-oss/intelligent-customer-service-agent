"""Real isolated-MySQL acceptance tests for R6-4G task recovery."""

import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pymysql


@unittest.skipUnless(os.getenv("RUN_R6G_MYSQL") == "1", "requires R6G isolated MySQL")
class TaskRecoveryMySQLTests(unittest.TestCase):
    DB_PREFIX = "phase5a_r6g_20261020_"
    UUID = "c162676c-c840-11f1-bff1-0a7c71283ea5"

    @classmethod
    def setUpClass(cls):
        database = os.getenv("MYSQL_DATABASE", "")
        if (
            (os.getenv("MYSQL_HOST"), os.getenv("MYSQL_PORT"))
            != ("127.0.0.1", "3312")
            or not database.startswith(cls.DB_PREFIX)
        ):
            raise RuntimeError("Refusing non-R6G isolated MySQL target")
        from db.connection import get_connection

        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT @@server_uuid AS value")
                if cursor.fetchone()["value"] != cls.UUID:
                    raise RuntimeError("Unexpected MySQL instance")
                cursor.execute("SHOW TABLES")
                if cursor.fetchall():
                    raise RuntimeError("Refusing nonempty isolated schema")
        finally:
            connection.close()

        from db.init_db import create_tables
        from db.migrate_action_receipts import migrate_action_receipts
        from db.migrate_task_execution import migrate_task_execution
        from db.migrate_task_recovery import migrate_task_recovery

        create_tables()
        migrate_action_receipts()
        migrate_task_execution()
        migrate_task_recovery()
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO users(username) VALUES ('r6g-owner'),('r6g-other')")
                cursor.execute("INSERT INTO conversations(user_id,title) VALUES (1,'r6g')")
                cursor.execute("""
                    INSERT INTO orders(order_no,user_id,total_amount,status)
                    VALUES ('R6G-ORDER',1,100,'delivered')
                """)
                cls.order_id = cursor.lastrowid
                cursor.execute("""
                    INSERT INTO order_items(order_id,product_name,quantity,unit_price)
                    VALUES (%s,'R6G item',1,100)
                """, (cls.order_id,))
                cls.item_id = cursor.lastrowid
            connection.commit()
        finally:
            connection.close()
        cls.baseline = cls.counts()
        print("R6G isolated baseline", cls.baseline)

    @classmethod
    def tearDownClass(cls):
        print("R6G isolated final", cls.counts())

    @classmethod
    def counts(cls):
        from db.connection import get_connection
        connection = get_connection()
        try:
            answer = {}
            with connection.cursor() as cursor:
                for table in (
                    "agent_tasks", "agent_task_steps", "agent_pending_actions",
                    "agent_action_receipts", "after_sales",
                ):
                    cursor.execute(f"SELECT COUNT(*) AS n FROM {table}")
                    answer[table] = cursor.fetchone()["n"]
            return answer
        finally:
            connection.close()

    def row(self, sql, args=()):
        from db.connection import get_connection
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, args)
                return cursor.fetchone()
        finally:
            connection.close()

    def execute(self, sql, args=()):
        from db.connection import get_connection
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, args)
                value = cursor.lastrowid
            connection.commit()
            return value
        finally:
            connection.close()

    def task(self, suffix, owner=None):
        from agent.conversation_scope import conversation_scope
        from services.task_service import create_agent_task
        with conversation_scope(1, 1):
            return create_agent_task(
                1, "agent", "r6g-" + suffix, "unknown", owner,
            )["task_id"]

    def expire(self, task_id, seconds=120):
        self.execute("""
            UPDATE agent_tasks
            SET execution_heartbeat_at=DATE_SUB(NOW(), INTERVAL %s SECOND)
            WHERE id=%s
        """, (seconds, task_id))

    def action(self, task_id, confirmation, status="pending", action_type="create_after_sale", preview=None):
        return self.execute("""
            INSERT INTO agent_pending_actions
                (confirmation_id,user_id,conversation_id,task_id,action_type,preview_json,status)
            VALUES (%s,1,1,%s,%s,%s,%s)
        """, (confirmation, task_id, action_type, json.dumps(preview or {}), status))

    def claim_and_recover(self, task_id, old_owner, recovery_id):
        from services.task_recovery_service import claim_task_recovery, recover_claimed_task
        candidate = {
            "id": task_id, "execution_id": old_owner,
            "recovery_id": None, "status": "running",
        }
        self.assertTrue(claim_task_recovery(candidate, recovery_id))
        return recover_claimed_task(task_id, recovery_id)

    def test_01_valid_lease_is_not_scanned(self):
        from services.task_recovery_service import list_task_recovery_candidates
        from services.task_service import update_agent_task_context
        task_id = self.task("valid", "EXEC-R6G-VALID")
        ids = {row["id"] for row in list_task_recovery_candidates(60, 60, 100)}
        self.assertNotIn(task_id, ids)
        update_agent_task_context(task_id, "completed", "completed", "EXEC-R6G-VALID")

    def test_02_expired_lease_is_atomically_claimed(self):
        from services.task_recovery_service import claim_task_recovery, recover_claimed_task
        task_id = self.task("expired", "EXEC-R6G-EXPIRED")
        self.expire(task_id)
        candidate = {"id": task_id, "execution_id": "EXEC-R6G-EXPIRED", "recovery_id": None, "status": "running"}
        self.assertTrue(claim_task_recovery(candidate, "REC-R6G-EXPIRED"))
        row = self.row("SELECT status,execution_id,recovery_source_execution_id FROM agent_tasks WHERE id=%s", (task_id,))
        self.assertEqual((row["status"], row["execution_id"]), ("recovering", "REC-R6G-EXPIRED"))
        self.assertEqual(row["recovery_source_execution_id"], "EXEC-R6G-EXPIRED")
        self.assertEqual(recover_claimed_task(task_id, "REC-R6G-EXPIRED"), "failed")

    def test_03_two_recovery_workers_only_one_wins(self):
        from services.task_recovery_service import claim_task_recovery, recover_claimed_task
        task_id = self.task("recovery-race", "EXEC-R6G-RACE")
        self.expire(task_id)
        candidate = {"id": task_id, "execution_id": "EXEC-R6G-RACE", "recovery_id": None, "status": "running"}
        barrier, answers = threading.Barrier(2), []
        def worker(recovery_id):
            barrier.wait()
            answers.append((recovery_id, claim_task_recovery(candidate, recovery_id)))
        threads = [threading.Thread(target=worker, args=(value,)) for value in ("REC-RACE-A", "REC-RACE-B")]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        winners = [value for value, won in answers if won]
        self.assertEqual(len(winners), 1)
        recover_claimed_task(task_id, winners[0])

    def test_04_stale_execution_cannot_overwrite_recovery(self):
        from services.task_recovery_service import claim_task_recovery, recover_claimed_task
        from services.task_service import TaskExecutionConflict, update_agent_task_context
        task_id = self.task("fenced-state", "EXEC-R6G-STALE")
        self.expire(task_id)
        candidate = {"id": task_id, "execution_id": "EXEC-R6G-STALE", "recovery_id": None, "status": "running"}
        self.assertTrue(claim_task_recovery(candidate, "REC-R6G-FENCE"))
        with self.assertRaises(TaskExecutionConflict):
            update_agent_task_context(task_id, "completed", "completed", "EXEC-R6G-STALE")
        recover_claimed_task(task_id, "REC-R6G-FENCE")

    def test_05_stale_execution_cannot_create_pending_action(self):
        from agent.confirmation_store import create_pending_action
        from agent.conversation_scope import conversation_scope, task_scope
        from services.task_recovery_service import claim_task_recovery, recover_claimed_task
        from services.task_service import TaskExecutionConflict
        task_id = self.task("fenced-action", "EXEC-R6G-ACTION")
        self.expire(task_id)
        candidate = {"id": task_id, "execution_id": "EXEC-R6G-ACTION", "recovery_id": None, "status": "running"}
        self.assertTrue(claim_task_recovery(candidate, "REC-R6G-ACTION"))
        with conversation_scope(1, 1), task_scope(task_id, "EXEC-R6G-ACTION"):
            with self.assertRaises(TaskExecutionConflict):
                create_pending_action(1, "create_after_sale", {"order_item_id": self.item_id}, task_id=task_id)
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM agent_pending_actions WHERE task_id=%s", (task_id,))["n"], 0)
        recover_claimed_task(task_id, "REC-R6G-ACTION")

    def test_06_interrupted_input_analysis_safely_converges(self):
        task_id = self.task("input-interrupted", "EXEC-R6G-INPUT")
        self.expire(task_id)
        self.assertEqual(self.claim_and_recover(task_id, "EXEC-R6G-INPUT", "REC-R6G-INPUT"), "failed")
        row = self.row("SELECT status,recovery_status,recovery_reason FROM agent_tasks WHERE id=%s", (task_id,))
        self.assertEqual((row["status"], row["recovery_status"]), ("failed", "failed"))
        self.assertEqual(row["recovery_reason"], "LEASE_EXPIRED_NO_BUSINESS_SIDE_EFFECT")

    def test_07_missing_trace_with_possible_business_result_requires_review(self):
        task_id = self.task("missing-trace", "EXEC-R6G-NOTRACE")
        self.action(task_id, "CONF-R6G-NOTRACE", "executing", "create_after_sale", {"order_item_id": self.item_id})
        self.expire(task_id)
        self.assertEqual(self.claim_and_recover(task_id, "EXEC-R6G-NOTRACE", "REC-R6G-NOTRACE"), "review_required")
        row = self.row("SELECT status,recovery_status FROM agent_tasks WHERE id=%s", (task_id,))
        self.assertEqual((row["status"], row["recovery_status"]), ("recovery_review", "review_required"))

    def test_08_pending_action_is_preserved_without_duplication(self):
        task_id = self.task("pending", "EXEC-R6G-PENDING")
        self.action(task_id, "CONF-R6G-PENDING", "pending")
        self.expire(task_id)
        before = self.row("SELECT COUNT(*) AS n FROM agent_pending_actions WHERE task_id=%s", (task_id,))["n"]
        self.assertEqual(self.claim_and_recover(task_id, "EXEC-R6G-PENDING", "REC-R6G-PENDING"), "waiting_confirm")
        after = self.row("SELECT COUNT(*) AS n FROM agent_pending_actions WHERE task_id=%s", (task_id,))["n"]
        self.assertEqual((before, after), (1, 1))
        self.assertEqual(self.row("SELECT status FROM agent_tasks WHERE id=%s", (task_id,))["status"], "waiting_confirm")

    def test_09_committed_create_is_verified_by_original_idempotency_key(self):
        task_id = self.task("create-proof", "EXEC-R6G-CREATE")
        preview = {"order_item_id": self.item_id, "after_sale_type": "repair"}
        self.action(task_id, "CONF-R6G-CREATE", "executing", "create_after_sale", preview)
        self.execute("""
            INSERT INTO after_sales
                (after_sale_no,user_id,order_id,order_item_id,type,reason,status,idempotency_key)
            VALUES ('AS-R6G-CREATE',1,%s,%s,'repair','r6g','pending','CONF-R6G-CREATE')
        """, (self.order_id, self.item_id))
        self.expire(task_id)
        self.assertEqual(self.claim_and_recover(task_id, "EXEC-R6G-CREATE", "REC-R6G-CREATE"), "completed")
        self.assertEqual(self.row("SELECT status FROM agent_pending_actions WHERE confirmation_id='CONF-R6G-CREATE'")["status"], "executed")
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM after_sales WHERE idempotency_key='CONF-R6G-CREATE'")["n"], 1)

    def test_10_r6e_receipt_safely_completes_cancel_operation(self):
        from services.after_sale_operation_service import _operation_fingerprint
        task_id = self.task("receipt", "EXEC-R6G-RECEIPT")
        preview = {"after_sale_id": 99991, "after_sale_no": "AS-R6G-RECEIPT"}
        self.action(task_id, "CONF-R6G-RECEIPT", "executing", "cancel_after_sale", preview)
        result = {"success": True, "code": "AFTER_SALE_CANCELLED", "data": {"after_sale_id": 99991}}
        self.execute("""
            INSERT INTO agent_action_receipts
                (confirmation_id,user_id,action_type,request_fingerprint,status,result_json)
            VALUES (%s,1,'cancel_after_sale',%s,'succeeded',%s)
        """, ("CONF-R6G-RECEIPT", _operation_fingerprint("cancel_after_sale", preview), json.dumps(result)))
        self.expire(task_id)
        self.assertEqual(self.claim_and_recover(task_id, "EXEC-R6G-RECEIPT", "REC-R6G-RECEIPT"), "completed")
        self.assertEqual(self.row("SELECT status FROM agent_pending_actions WHERE confirmation_id='CONF-R6G-RECEIPT'")["status"], "executed")

    def test_11_unknown_operation_result_remains_for_review(self):
        task_id = self.task("unknown", "EXEC-R6G-UNKNOWN")
        self.action(task_id, "CONF-R6G-UNKNOWN", "executing", "change_after_sale_type", {
            "after_sale_id": 99992, "after_sale_no": "AS-R6G-UNKNOWN",
            "old_type": "return_refund", "new_type": "repair",
        })
        self.expire(task_id)
        self.assertEqual(self.claim_and_recover(task_id, "EXEC-R6G-UNKNOWN", "REC-R6G-UNKNOWN"), "review_required")
        self.assertEqual(self.row("SELECT status FROM agent_pending_actions WHERE confirmation_id='CONF-R6G-UNKNOWN'")["status"], "executing")

    def test_12_transient_scan_failure_never_fakes_terminal_and_later_recovers(self):
        from services.task_recovery_service import recover_expired_agent_tasks
        task_id = self.task("db-outage", "EXEC-R6G-DB")
        self.expire(task_id)
        with patch(
            "services.task_recovery_service.list_task_recovery_candidates",
            side_effect=pymysql.err.OperationalError(2006, "injected outage"),
        ):
            with self.assertRaises(pymysql.err.OperationalError):
                recover_expired_agent_tasks(lease_seconds=60, recovery_claim_seconds=60, batch_size=20, db_attempts=2)
        self.assertEqual(self.row("SELECT status FROM agent_tasks WHERE id=%s", (task_id,))["status"], "running")
        summary = recover_expired_agent_tasks(lease_seconds=60, recovery_claim_seconds=60, batch_size=20)
        self.assertGreaterEqual(summary["claimed"], 1)
        self.assertEqual(self.row("SELECT status FROM agent_tasks WHERE id=%s", (task_id,))["status"], "failed")

    def test_13_repeated_recovery_scan_has_no_extra_side_effect(self):
        from services.task_recovery_service import recover_expired_agent_tasks
        before = self.counts()
        first = recover_expired_agent_tasks(lease_seconds=60, recovery_claim_seconds=60, batch_size=100)
        middle = self.counts()
        second = recover_expired_agent_tasks(lease_seconds=60, recovery_claim_seconds=60, batch_size=100)
        after = self.counts()
        self.assertEqual(second["claimed"], 0)
        self.assertEqual(middle, after)
        self.assertGreaterEqual(first["claimed"], 0)
        self.assertGreaterEqual(middle["agent_task_steps"], before["agent_task_steps"])

    def test_14_historical_task_without_reliable_owner_is_not_recovered(self):
        task_id = self.task("historical", None)
        self.execute("UPDATE agent_tasks SET status='running', execution_heartbeat_at=NULL WHERE id=%s", (task_id,))
        from services.task_recovery_service import recover_expired_agent_tasks
        recover_expired_agent_tasks(lease_seconds=1, recovery_claim_seconds=1, batch_size=100)
        row = self.row("SELECT status,recovery_id FROM agent_tasks WHERE id=%s", (task_id,))
        self.assertEqual((row["status"], row["recovery_id"]), ("running", None))

    def test_15_recovery_and_confirm_atomic_claim_do_not_duplicate_action(self):
        from agent.confirmation_store import claim_pending_action_execution
        task_id = self.task("confirm", "EXEC-R6G-CONFIRM")
        self.action(task_id, "CONF-R6G-CONFIRM", "pending")
        self.expire(task_id)
        self.assertEqual(self.claim_and_recover(task_id, "EXEC-R6G-CONFIRM", "REC-R6G-CONFIRM"), "waiting_confirm")
        self.execute("UPDATE agent_pending_actions SET status='confirmed' WHERE confirmation_id='CONF-R6G-CONFIRM'")
        answers = []
        barrier = threading.Barrier(2)
        def worker():
            barrier.wait(); answers.append(claim_pending_action_execution("CONF-R6G-CONFIRM"))
        threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(answers), [False, True])
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM agent_pending_actions WHERE confirmation_id='CONF-R6G-CONFIRM'")["n"], 1)

    def test_16_renewed_heartbeat_wins_over_stale_scan_snapshot(self):
        from services.task_recovery_service import claim_task_recovery
        from services.task_service import renew_agent_task_lease
        task_id = self.task("heartbeat-race", "EXEC-R6G-HEARTBEAT")
        # A stale scanner snapshot is not authority to claim. Renew only a
        # still-valid owner; expired owners must never resurrect themselves.
        candidate = {
            "id": task_id, "execution_id": "EXEC-R6G-HEARTBEAT",
            "recovery_id": None, "status": "running",
        }
        self.assertTrue(renew_agent_task_lease(task_id, "EXEC-R6G-HEARTBEAT"))
        self.assertFalse(claim_task_recovery(candidate, "REC-R6G-TOO-LATE", 60, 60))
        row = self.row("SELECT status,execution_id FROM agent_tasks WHERE id=%s", (task_id,))
        self.assertEqual((row["status"], row["execution_id"]), ("running", "EXEC-R6G-HEARTBEAT"))
        from services.task_service import update_agent_task_context
        update_agent_task_context(task_id, "completed", "completed", "EXEC-R6G-HEARTBEAT")

    def test_17_crashed_recovery_worker_can_be_safely_reclaimed(self):
        from services.task_recovery_service import claim_task_recovery, recover_claimed_task
        task_id = self.task("recovery-crash", "EXEC-R6G-RECOVERY-CRASH")
        self.expire(task_id)
        first = {
            "id": task_id, "execution_id": "EXEC-R6G-RECOVERY-CRASH",
            "recovery_id": None, "status": "running",
        }
        self.assertTrue(claim_task_recovery(first, "REC-R6G-FIRST", 60, 60))
        self.execute("""
            UPDATE agent_tasks
            SET recovery_started_at=DATE_SUB(NOW(), INTERVAL 120 SECOND)
            WHERE id=%s
        """, (task_id,))
        stale_recovery = {
            "id": task_id, "execution_id": "REC-R6G-FIRST",
            "recovery_id": "REC-R6G-FIRST", "status": "recovering",
        }
        self.assertTrue(claim_task_recovery(stale_recovery, "REC-R6G-SECOND", 60, 60))
        self.assertEqual(recover_claimed_task(task_id, "REC-R6G-FIRST"), "skipped")
        self.assertEqual(recover_claimed_task(task_id, "REC-R6G-SECOND"), "failed")

    def test_18_recovery_racing_confirm_preserves_atomic_claim(self):
        from agent.confirmation_store import (
            claim_pending_action_execution,
            update_pending_action_status,
        )
        from services.task_recovery_service import claim_task_recovery, recover_claimed_task
        task_id = self.task("confirm-race", "EXEC-R6G-CONFIRM-RACE")
        self.action(task_id, "CONF-R6G-CONFIRM-RACE", "pending")
        self.expire(task_id)
        candidate = {
            "id": task_id, "execution_id": "EXEC-R6G-CONFIRM-RACE",
            "recovery_id": None, "status": "running",
        }
        barrier, answers, errors = threading.Barrier(2), [], []

        def recovery_worker():
            try:
                barrier.wait()
                if claim_task_recovery(candidate, "REC-R6G-CONFIRM-RACE", 60, 60):
                    answers.append(("recovery", recover_claimed_task(
                        task_id, "REC-R6G-CONFIRM-RACE",
                    )))
                else:
                    answers.append(("recovery", "not_claimed"))
            except Exception as exc:  # pragma: no cover - reported by assertion
                errors.append(exc)

        def confirm_worker():
            try:
                barrier.wait()
                confirmed = update_pending_action_status(
                    "CONF-R6G-CONFIRM-RACE", "confirmed",
                )
                answers.append(("confirm", bool(
                    confirmed and claim_pending_action_execution(
                        "CONF-R6G-CONFIRM-RACE",
                    )
                )))
            except Exception as exc:  # pragma: no cover - reported by assertion
                errors.append(exc)

        threads = [threading.Thread(target=recovery_worker), threading.Thread(target=confirm_worker)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(self.row(
            "SELECT COUNT(*) AS n FROM agent_pending_actions "
            "WHERE confirmation_id='CONF-R6G-CONFIRM-RACE'"
        )["n"], 1)
        action_status = self.row(
            "SELECT status FROM agent_pending_actions "
            "WHERE confirmation_id='CONF-R6G-CONFIRM-RACE'"
        )["status"]
        self.assertIn(action_status, {"confirmed", "executing"})
        task_status = self.row(
            "SELECT status FROM agent_tasks WHERE id=%s", (task_id,)
        )["status"]
        self.assertIn(task_status, {"waiting_confirm", "recovery_review"})

    def test_19_application_lifespan_automatically_runs_recovery_scan(self):
        from fastapi.testclient import TestClient
        import main
        from services.task_service import unregister_agent_task_lease
        task_id = self.task("lifespan-auto", "EXEC-R6G-LIFESPAN")
        unregister_agent_task_lease(task_id, "EXEC-R6G-LIFESPAN")
        self.expire(task_id)
        settings = SimpleNamespace(
            task_recovery_batch_size=20,
            task_heartbeat_interval_seconds=1,
            task_recovery_scan_interval_seconds=1,
            task_lease_seconds=60,
            task_recovery_claim_seconds=60,
        )
        with patch("main.get_settings", return_value=settings), TestClient(main.app):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if self.row(
                    "SELECT status FROM agent_tasks WHERE id=%s", (task_id,)
                )["status"] == "failed":
                    break
                time.sleep(0.1)
        self.assertEqual(
            self.row("SELECT status FROM agent_tasks WHERE id=%s", (task_id,))["status"],
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
