"""Real MySQL transaction tests for post-application operation receipts."""
import os
import threading
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.getenv("RUN_R6E_MYSQL") == "1", "requires R6E isolated MySQL")
class ActionReceiptMySQLTests(unittest.TestCase):
    DB_PREFIX = "phase5a_r6e_20261020_b"
    UUID = "c162676c-c840-11f1-bff1-0a7c71283ea5"

    @classmethod
    def setUpClass(cls):
        database = os.getenv("MYSQL_DATABASE", "")
        if ((os.getenv("MYSQL_HOST"), os.getenv("MYSQL_PORT")) != ("127.0.0.1", "3312")
                or not database.startswith(cls.DB_PREFIX)):
            raise RuntimeError("Refusing non-R6E isolated MySQL target")
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
        create_tables(); migrate_action_receipts(); migrate_task_execution(); migrate_task_recovery()
        connection = get_connection()
        try:
            with connection.cursor() as c:
                c.execute("INSERT INTO users(username) VALUES ('r6e-owner'),('r6e-other')")
                cls.owner, cls.other = 1, 2
                c.execute("INSERT INTO orders(order_no,user_id,total_amount,status) VALUES ('R6E-ORDER',1,100,'delivered')")
                cls.order_id = c.lastrowid
                c.execute("INSERT INTO order_items(order_id,product_name,quantity,unit_price) VALUES (%s,'R6E item',1,100)", (cls.order_id,))
                cls.item_id = c.lastrowid
            connection.commit()
        finally:
            connection.close()

    def sale(self, suffix, sale_type="return_refund"):
        from db.connection import get_connection
        con = get_connection()
        try:
            with con.cursor() as c:
                c.execute("""INSERT INTO after_sales
                    (after_sale_no,user_id,order_id,order_item_id,type,reason,status)
                    VALUES (%s,1,%s,%s,%s,'r6e','pending')""",
                    ("R6E-" + suffix, self.order_id, self.item_id, sale_type))
                answer = (c.lastrowid, "R6E-" + suffix)
            con.commit(); return answer
        finally:
            con.close()

    def call(self, action, sale_id, sale_no, confirmation, **extra):
        from services.after_sale_operation_service import execute_after_sale_operation
        return execute_after_sale_operation(user_id=1, conversation_id=1, action_type=action,
            confirmation_id=confirmation, preview={"after_sale_id": sale_id, "after_sale_no": sale_no, **extra})

    def value(self, sql, args):
        from db.connection import get_connection
        con = get_connection()
        try:
            with con.cursor() as c:
                c.execute(sql, args); return c.fetchone()
        finally:
            con.close()

    def test_01_cancel_commits_sale_and_receipt_and_replays(self):
        sale, number = self.sale("CANCEL")
        first = self.call("cancel_after_sale", sale, number, "R6E-CANCEL")
        replay = self.call("cancel_after_sale", sale, number, "R6E-CANCEL")
        self.assertTrue(first["success"]); self.assertEqual(first, replay)
        self.assertEqual(self.value("SELECT status FROM after_sales WHERE id=%s", (sale,))["status"], "cancelled")
        self.assertEqual(self.value("SELECT status FROM agent_action_receipts WHERE confirmation_id=%s", ("R6E-CANCEL",))["status"], "succeeded")

    def test_02_change_commits_sale_and_receipt_and_replays(self):
        sale, number = self.sale("CHANGE")
        args = {"old_type": "return_refund", "new_type": "repair"}
        first = self.call("change_after_sale_type", sale, number, "R6E-CHANGE", **args)
        replay = self.call("change_after_sale_type", sale, number, "R6E-CHANGE", **args)
        self.assertTrue(first["success"]); self.assertEqual(first, replay)
        self.assertEqual(self.value("SELECT type FROM after_sales WHERE id=%s", (sale,))["type"], "repair")

    def test_03_receipt_write_error_rolls_back_sale_change(self):
        sale, number = self.sale("ROLLBACK")
        with patch("services.after_sale_operation_service._store_receipt", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError): self.call("cancel_after_sale", sale, number, "R6E-ROLLBACK")
        self.assertEqual(self.value("SELECT status FROM after_sales WHERE id=%s", (sale,))["status"], "pending")
        self.assertIsNone(self.value("SELECT confirmation_id FROM agent_action_receipts WHERE confirmation_id=%s", ("R6E-ROLLBACK",)))

    def test_04_different_user_and_incompatible_payload_are_rejected(self):
        sale, number = self.sale("IDENTITY")
        self.call("cancel_after_sale", sale, number, "R6E-IDENTITY")
        from services.after_sale_operation_service import execute_after_sale_operation
        with self.assertRaises(ValueError):
            execute_after_sale_operation(user_id=2, conversation_id=1, action_type="cancel_after_sale", confirmation_id="R6E-IDENTITY", preview={"after_sale_id": sale, "after_sale_no": number})
        with self.assertRaises(ValueError):
            self.call("change_after_sale_type", sale, number, "R6E-IDENTITY", old_type="return_refund", new_type="repair")
        with self.assertRaises(ValueError):
            self.call("cancel_after_sale", sale, number, "R6E-IDENTITY", cancel_reason="different request")

    def test_05_concurrent_same_confirmation_executes_once(self):
        sale, number = self.sale("CONCURRENT")
        answers, errors = [], []
        def worker():
            try: answers.append(self.call("cancel_after_sale", sale, number, "R6E-CONCURRENT"))
            except Exception as exc: errors.append(exc)
        one, two = threading.Thread(target=worker), threading.Thread(target=worker)
        one.start(); two.start(); one.join(); two.join()
        self.assertFalse(errors); self.assertEqual(len(answers), 2); self.assertEqual(answers[0], answers[1])
        self.assertEqual(self.value("SELECT COUNT(*) AS n FROM agent_action_receipts WHERE confirmation_id=%s", ("R6E-CONCURRENT",))["n"], 1)

    def test_06_original_id_can_be_checked_without_new_identity(self):
        sale, number = self.sale("LOOKUP")
        first = self.call("cancel_after_sale", sale, number, "R6E-LOOKUP")
        from services.after_sale_operation_service import get_operation_receipt
        checked = get_operation_receipt("R6E-LOOKUP", 1, "cancel_after_sale", {"after_sale_id": sale, "after_sale_no": number})
        self.assertEqual(checked, first)

    def test_07_executing_action_recovers_only_from_original_receipt(self):
        sale, number = self.sale("RECOVER")
        original = self.call("cancel_after_sale", sale, number, "R6E-RECOVER")
        from services.task_service import create_agent_task, update_agent_task_context
        task_id = create_agent_task(
            1, "agent", "r6e receipt recovery", "cancel_after_sale", "EXEC-R6E-RECOVER",
        )["task_id"]
        update_agent_task_context(
            task_id, "waiting_confirm", "waiting_confirm", "EXEC-R6E-RECOVER",
        )
        from db.connection import get_connection
        import json
        con = get_connection()
        try:
            with con.cursor() as c:
                c.execute("""INSERT INTO agent_pending_actions
                    (confirmation_id,user_id,conversation_id,task_id,action_type,preview_json,status)
                    VALUES ('R6E-RECOVER',1,NULL,%s,'cancel_after_sale',%s,'executing')""",
                    (task_id, json.dumps({"after_sale_id": sale, "after_sale_no": number})))
            con.commit()
        finally:
            con.close()
        from agent.action_executor import execute_confirmed_action
        recovered = execute_confirmed_action("R6E-RECOVER", 1, "r6e-recover")
        self.assertTrue(recovered["success"])
        self.assertEqual(recovered["data"]["result"], original)
        self.assertEqual(self.value("SELECT status FROM agent_pending_actions WHERE confirmation_id=%s", ("R6E-RECOVER",))["status"], "executed")

    def test_08_rejected_operation_does_not_commit_processing_receipt(self):
        sale, number = self.sale("REJECT")
        self.call("cancel_after_sale", sale, number, "R6E-REJECT-FIRST")
        rejected = self.call("cancel_after_sale", sale, number, "R6E-REJECT-SECOND")
        self.assertFalse(rejected["success"])
        self.assertEqual(rejected["code"], "AFTER_SALE_ALREADY_CANCELLED")
        self.assertIsNone(self.value(
            "SELECT confirmation_id FROM agent_action_receipts WHERE confirmation_id=%s",
            ("R6E-REJECT-SECOND",),
        ))
