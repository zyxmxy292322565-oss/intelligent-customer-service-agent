"""Real MySQL concurrency tests for database-backed Task execution ownership."""

import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipUnless(os.getenv("RUN_R6F_MYSQL") == "1", "requires R6F isolated MySQL")
class TaskExecutionMySQLTests(unittest.TestCase):
    DB_PREFIX = "phase5a_r6f_20261020_"
    UUID = "c162676c-c840-11f1-bff1-0a7c71283ea5"

    @classmethod
    def setUpClass(cls):
        database = os.getenv("MYSQL_DATABASE", "")
        if ((os.getenv("MYSQL_HOST"), os.getenv("MYSQL_PORT")) != ("127.0.0.1", "3312")
                or not database.startswith(cls.DB_PREFIX)):
            raise RuntimeError("Refusing non-R6F isolated MySQL target")
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
                cursor.execute("INSERT INTO users(username) VALUES ('r6f-owner'),('r6f-other')")
                cursor.execute(
                    "INSERT INTO conversations(user_id,title) VALUES (1,'r6f-one'),(1,'r6f-two')"
                )
                cls.conversation_one = 1
                cls.conversation_two = 2
            connection.commit()
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

    def task(self, suffix: str, execution_id: str | None = None, conversation_id=None):
        from agent.conversation_scope import conversation_scope
        from services.task_service import create_agent_task
        if conversation_id is None:
            return create_agent_task(1, "agent", "r6f-" + suffix, "unknown", execution_id)["task_id"]
        with conversation_scope(1, conversation_id):
            return create_agent_task(1, "agent", "r6f-" + suffix, "unknown", execution_id)["task_id"]

    def insert_action(self, task_id: int, confirmation_id: str, status: str):
        from db.connection import get_connection
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO agent_pending_actions
                        (confirmation_id,user_id,task_id,action_type,preview_json,status,
                         execution_result_json)
                    VALUES (%s,1,%s,'cancel_after_sale','{}',%s,%s)
                """, (
                    confirmation_id, task_id, status,
                    json.dumps({"success": status == "executed"}),
                ))
            connection.commit()
        finally:
            connection.close()

    def test_01_two_claimants_only_one_gets_task(self):
        from services.task_service import claim_agent_task_execution, update_agent_task_context
        task_id = self.task("race")
        barrier = threading.Barrier(2)
        answers = []

        def worker(owner):
            barrier.wait()
            answers.append((owner, claim_agent_task_execution(task_id, 1, None, owner)))

        threads = [threading.Thread(target=worker, args=(owner,)) for owner in ("EXEC-RACE-A", "EXEC-RACE-B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [owner for owner, claimed in answers if claimed]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.row("SELECT execution_id FROM agent_tasks WHERE id=%s", (task_id,))["execution_id"], winners[0])
        update_agent_task_context(task_id, "completed", "completed", winners[0])

    def test_02_old_execution_cannot_overwrite_new_execution(self):
        from services.task_service import (
            TaskExecutionConflict, claim_agent_task_execution, update_agent_task_context,
        )
        task_id = self.task("old-new", "EXEC-OLD")
        update_agent_task_context(task_id, "waiting_input", "waiting_input", "EXEC-OLD")
        self.assertTrue(claim_agent_task_execution(task_id, 1, None, "EXEC-NEW"))
        with self.assertRaises(TaskExecutionConflict):
            update_agent_task_context(task_id, "failed", "failed", "EXEC-OLD")
        row = self.row("SELECT status,execution_id FROM agent_tasks WHERE id=%s", (task_id,))
        self.assertEqual((row["status"], row["execution_id"]), ("running", "EXEC-NEW"))
        update_agent_task_context(task_id, "completed", "completed", "EXEC-NEW")

    def test_03_completed_task_rejects_stale_execution(self):
        from services.task_service import TaskExecutionConflict, update_agent_task_context
        task_id = self.task("complete", "EXEC-COMPLETE")
        update_agent_task_context(task_id, "completed", "completed", "EXEC-COMPLETE")
        with self.assertRaises(TaskExecutionConflict):
            update_agent_task_context(task_id, "failed", "failed", "EXEC-COMPLETE")
        self.assertEqual(self.row("SELECT status FROM agent_tasks WHERE id=%s", (task_id,))["status"], "completed")

    def test_04_waiting_input_releases_and_preserves_context(self):
        from services.task_service import (
            claim_agent_task_execution, get_task_context, update_agent_task_context,
            update_task_context,
        )
        task_id = self.task("multiturn", "EXEC-TURN-1")
        update_task_context(task_id, {"product_name": "AirPods Pro 2"}, "EXEC-TURN-1")
        update_agent_task_context(task_id, "waiting_input", "waiting_input", "EXEC-TURN-1")
        self.assertTrue(claim_agent_task_execution(task_id, 1, None, "EXEC-TURN-2"))
        self.assertEqual(get_task_context(task_id, "EXEC-TURN-2")["product_name"], "AirPods Pro 2")
        update_task_context(task_id, {"product_name": "AirPods Pro 2", "after_sale_type": "repair"}, "EXEC-TURN-2")
        row = self.row("SELECT context,execution_id FROM agent_tasks WHERE id=%s", (task_id,))
        self.assertEqual(row["execution_id"], "EXEC-TURN-2")
        update_agent_task_context(task_id, "completed", "completed", "EXEC-TURN-2")

    def test_05_old_runtime_and_graph_reacquire_waiting_input(self):
        from agent.conversation_scope import conversation_scope
        from agent.runtime import run_agent
        from agent.graph.runtime import run_graph_agent

        analyses = SimpleNamespace(
            intent="create_after_sale",
            params=SimpleNamespace(product_name="R6F item", after_sale_type=None, reason=None),
        )
        waiting = {"success": False, "code": "AFTER_SALE_TYPE_REQUIRED", "message": "补充类型"}
        with conversation_scope(1, self.conversation_one), \
             patch("agent.runtime.analyze_agent_input", return_value=analyses), \
             patch("agent.runtime.run_write_workflow", side_effect=[waiting, {"success": True, "code": "OK"}]):
            self.assertEqual(run_agent("first", 1, "R6F-OLD-1")["code"], "AFTER_SALE_TYPE_REQUIRED")
            self.assertEqual(run_agent("second", 1, "R6F-OLD-2")["code"], "OK")
        old_rows = self.row(
            "SELECT COUNT(*) AS n FROM agent_tasks WHERE conversation_id=%s", (self.conversation_one,),
        )["n"]
        self.assertEqual(old_rows, 1)

        calls = []
        def graph_core(*args, **kwargs):
            calls.append(1)
            args[6].update({"product_name": "R6F graph"})
            return waiting if len(calls) == 1 else {"success": True, "code": "OK", "message": "done"}
        with conversation_scope(1, self.conversation_two), \
             patch("agent.graph.runtime._run_graph_agent_core", side_effect=graph_core), \
             patch("agent.graph.runtime.trace_step") as trace:
            trace.return_value.__enter__.return_value.set_result.return_value = None
            self.assertEqual(run_graph_agent("first", 1, "R6F-GRAPH-1")["code"], "AFTER_SALE_TYPE_REQUIRED")
            self.assertEqual(run_graph_agent("second", 1, "R6F-GRAPH-2")["code"], "OK")
        graph_rows = self.row(
            "SELECT COUNT(*) AS n FROM agent_tasks WHERE conversation_id=%s", (self.conversation_two,),
        )["n"]
        self.assertEqual(graph_rows, 1)

    def test_06_confirm_finalizer_only_updates_unowned_waiting_task(self):
        from agent.confirmation_store import update_pending_action_execution
        from services.task_service import (
            TaskExecutionConflict, claim_agent_task_execution, update_agent_task_context,
        )
        task_id = self.task("confirm", "EXEC-PREVIEW")
        update_agent_task_context(task_id, "waiting_confirm", "waiting_confirm", "EXEC-PREVIEW")
        self.assertFalse(claim_agent_task_execution(task_id, 1, None, "EXEC-TOO-LATE"))
        self.insert_action(task_id, "CONF-R6F", "executing")
        result = update_pending_action_execution("CONF-R6F", "executed", {"success": True})
        self.assertEqual(result["status"], "executed")
        self.assertEqual(self.row("SELECT status FROM agent_tasks WHERE id=%s", (task_id,))["status"], "completed")
        with self.assertRaises(TaskExecutionConflict):
            update_agent_task_context(task_id, "failed", "failed", "EXEC-PREVIEW")

    def test_07_reconciler_skips_task_with_live_owner(self):
        from services.task_service import reconcile_finished_agent_tasks, update_agent_task_context
        task_id = self.task("coordinator", "EXEC-LIVE")
        self.insert_action(task_id, "CONF-R6F-RECON", "executed")
        self.assertEqual(reconcile_finished_agent_tasks(), 0)
        row = self.row("SELECT status,execution_id FROM agent_tasks WHERE id=%s", (task_id,))
        self.assertEqual((row["status"], row["execution_id"]), ("running", "EXEC-LIVE"))
        update_agent_task_context(task_id, "completed", "completed", "EXEC-LIVE")

    def test_08_context_update_cannot_erase_or_bypass_owner(self):
        from services.task_service import TaskExecutionConflict, update_agent_task_context, update_task_context
        task_id = self.task("context", "EXEC-CONTEXT")
        update_task_context(task_id, {"reason": "quality"}, "EXEC-CONTEXT")
        self.assertEqual(self.row("SELECT execution_id FROM agent_tasks WHERE id=%s", (task_id,))["execution_id"], "EXEC-CONTEXT")
        with self.assertRaises(TaskExecutionConflict):
            update_task_context(task_id, {"reason": "stale"}, "EXEC-STALE")
        update_agent_task_context(task_id, "completed", "completed", "EXEC-CONTEXT")


if __name__ == "__main__":
    unittest.main()
