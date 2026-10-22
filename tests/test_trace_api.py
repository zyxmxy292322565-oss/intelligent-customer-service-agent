"""JWT-protected, read-only Agent trace endpoints."""

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import main
from auth.jwt import create_access_token
from services.trace_service import get_user_task_trace, list_user_tasks


class TraceAPITests(unittest.TestCase):
    def setUp(self):
        self.global_limit = patch("middleware.rate_limit.global_limiter.allow", return_value=True)
        self.user_limit = patch("middleware.rate_limit.user_limiter.allow", return_value=True)
        self.global_limit.start()
        self.user_limit.start()
        self.addCleanup(self.global_limit.stop)
        self.addCleanup(self.user_limit.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    @staticmethod
    def headers(user_id: int) -> dict:
        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    def test_trace_endpoints_require_jwt(self):
        with patch("main.list_user_tasks") as tasks, patch("main.get_user_task_trace") as trace:
            self.assertEqual(self.client.get("/agent/tasks").status_code, 401)
            self.assertEqual(self.client.get("/agent/trace/42").status_code, 401)
        tasks.assert_not_called()
        trace.assert_not_called()

    def test_task_list_uses_token_user(self):
        with patch("main.list_user_tasks", return_value=[{"id": 42}]) as tasks:
            response = self.client.get("/agent/tasks", headers=self.headers(7))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["tasks"], [{"id": 42}])
        tasks.assert_called_once_with(7)

    def test_owned_trace_and_other_user_404(self):
        def owned_trace(user_id, task_id):
            if user_id == 7 and task_id == 42:
                return {"task": {"id": 42}, "steps": [{"step_name": "input_analysis"}]}
            return None

        with patch("main.get_user_task_trace", side_effect=owned_trace) as trace:
            own = self.client.get("/agent/trace/42", headers=self.headers(7))
            other = self.client.get("/agent/trace/42", headers=self.headers(8))
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.json()["data"]["steps"][0]["step_name"], "input_analysis")
        self.assertEqual(other.status_code, 404)
        self.assertEqual(trace.call_args_list[0].args, (7, 42))
        self.assertEqual(trace.call_args_list[1].args, (8, 42))


class TraceServiceTests(unittest.TestCase):
    @patch("services.trace_service.get_connection")
    def test_list_is_read_only_and_scoped(self, connect):
        connection = MagicMock()
        connect.return_value = connection
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{"id": 42}]
        self.assertEqual(list_user_tasks(7), [{"id": 42}])
        self.assertEqual(cursor.execute.call_args_list[0].args, ("SET TRANSACTION READ ONLY",))
        self.assertIn("WHERE user_id=%s", cursor.execute.call_args_list[1].args[0])
        self.assertEqual(cursor.execute.call_args_list[1].args[1], (7,))
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    @patch("services.trace_service.get_connection")
    def test_other_user_steps_are_never_read(self, connect):
        connection = MagicMock()
        connect.return_value = connection
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        self.assertIsNone(get_user_task_trace(8, 42))
        self.assertIn("WHERE id=%s AND user_id=%s", cursor.execute.call_args_list[1].args[0])
        self.assertEqual(cursor.execute.call_args_list[1].args[1], (42, 8))
        self.assertEqual(cursor.execute.call_count, 2)
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
