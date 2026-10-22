"""P4 HTTP 身份边界测试；数据库、Redis 和业务执行均使用 mock。"""

from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import main
from auth.jwt import create_access_token


class JwtAuthTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("list_conversations", [{"id": 1}]),
            ("get_conversation", {"id": 1}),
            ("save_message", None),
        ):
            service_patch = patch(f"main.{name}", return_value=value)
            service_patch.start()
            self.addCleanup(service_patch.stop)
        self.global_limiter = patch("middleware.rate_limit.global_limiter.allow", return_value=True)
        self.user_limiter = patch("middleware.rate_limit.user_limiter.allow", return_value=True)
        self.global_limiter.start()
        self.user_limiter.start()
        self.addCleanup(self.global_limiter.stop)
        self.addCleanup(self.user_limiter.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    @staticmethod
    def headers(user_id=1):
        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    def test_mock_login_issues_token_for_existing_user(self):
        connection = MagicMock()
        with patch("main.get_settings", return_value=SimpleNamespace(app_env="test")), \
             patch("main.get_connection", return_value=connection), \
             patch("main.get_user_by_id", return_value={"id": 1}):
            response = self.client.post("/auth/login", json={"user_id": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token_type"], "bearer")
        token = response.json()["access_token"]
        with patch("main.get_user_orders_service", return_value={"orders": []}) as orders:
            result = self.client.get("/users/1/orders", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(result.status_code, 200)
        orders.assert_called_once_with(1)
        connection.close.assert_called_once()

    def test_body_user_id_cannot_override_token_for_agent(self):
        with patch("middleware.rate_limit.user_limiter.allow", return_value=True) as user_limit, \
             patch("main.run_agent", return_value={
            "success": True, "code": "OK", "message": "ok"
        }) as run_agent:
            response = self.client.post(
                "/agent/chat", json={"user_id": 2, "message": "查我的订单"},
                headers={**self.headers(1), "user-id": "2"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run_agent.call_args.kwargs["current_user_id"], 1)
        self.assertTrue(user_limit.call_args.args[0].endswith(":rate_limit:user:1"))

    def test_token_without_legacy_body_user_id_is_accepted(self):
        with patch("main.run_agent", return_value={
            "success": True, "code": "OK", "message": "ok"
        }) as run_agent:
            response = self.client.post(
                "/agent/chat", json={"message": "查我的订单"}, headers=self.headers(1)
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run_agent.call_args.kwargs["current_user_id"], 1)

    def test_missing_token_returns_401_before_agent_execution(self):
        with patch("main.run_agent") as run_agent:
            response = self.client.post(
                "/agent/chat", json={"user_id": 1, "message": "查我的订单"},
                headers={"user-id": "1"},
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        run_agent.assert_not_called()
        self.assertEqual(self.client.get("/users/1/orders").status_code, 401)
        self.assertEqual(self.client.post(
            "/agent/confirm", json={"confirmation_id": "CONF_1", "user_id": 1}
        ).status_code, 401)
        self.assertEqual(self.client.post(
            "/agent/cancel", json={"confirmation_id": "CONF_1", "user_id": 1}
        ).status_code, 401)

    def test_expired_token_returns_401(self):
        token = create_access_token(1, expires_delta=timedelta(seconds=-1))
        with patch("main.run_agent") as run_agent:
            response = self.client.post(
                "/agent/chat", json={"message": "查我的订单"},
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 401)
        run_agent.assert_not_called()

    def test_modified_signature_returns_401(self):
        token = create_access_token(1)
        header, payload, signature = token.split(".")
        changed_signature = ("a" if signature[0] != "a" else "b") + signature[1:]
        changed = ".".join((header, payload, changed_signature))
        response = self.client.get("/users/1/orders", headers={"Authorization": f"Bearer {changed}"})
        self.assertEqual(response.status_code, 401)

    def test_token_cannot_read_other_users_orders(self):
        with patch("main.get_user_orders_service") as orders:
            response = self.client.get("/users/2/orders", headers=self.headers(1))
        self.assertEqual(response.status_code, 403)
        orders.assert_not_called()

    def test_token_cannot_confirm_other_users_action(self):
        action = {"confirmation_id": "CONF_USER_2", "user_id": 2, "status": "pending"}

        def owned_action(confirmation_id, current_user_id):
            return action if current_user_id == action["user_id"] else None

        with patch("main.get_pending_action", side_effect=owned_action), \
             patch("agent.confirmation_service.get_pending_action", side_effect=owned_action), \
             patch("agent.confirmation_service.expire_pending_action_if_due", return_value=False), \
             patch("agent.step_trace._start_step", return_value=1), \
             patch("agent.step_trace._finish_step"), \
             patch("main.execute_confirmed_action") as execute:
            response = self.client.post(
                "/agent/confirm",
                json={"user_id": 2, "confirmation_id": "CONF_USER_2"},
                headers=self.headers(1),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], "ACTION_NOT_FOUND")
        execute.assert_not_called()

    def test_cancel_uses_token_owner_not_body_user_id(self):
        with patch("main.cancel_action", return_value={
            "success": False, "code": "ACTION_NOT_FOUND", "message": "待确认操作不存在"
        }) as cancel:
            response = self.client.post(
                "/agent/cancel",
                json={"user_id": 2, "confirmation_id": "CONF_USER_2"},
                headers=self.headers(1),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], "ACTION_NOT_FOUND")
        cancel.assert_called_once_with("CONF_USER_2", 1)

    def test_after_sale_creation_uses_token_user_id(self):
        with patch("main.create_after_sale_service", return_value={"success": True}) as service:
            response = self.client.post("/after-sales", json={
                "user_id": 2, "order_no": "ORDER_1", "order_item_id": 3,
                "type": "return_refund", "reason": "质量不好",
            }, headers=self.headers(1))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.call_args.kwargs["user_id"], 1)

    def test_other_users_after_sale_status_cannot_be_changed(self):
        with patch("main.get_connection", return_value=MagicMock()), \
             patch("main.get_after_sale_by_id", return_value={"user_id": 2}), \
             patch("main.update_after_sale_status_service") as service:
            response = self.client.patch(
                "/after-sales/5/status", json={"new_status": "processing"},
                headers=self.headers(1),
            )
        self.assertEqual(response.status_code, 404)
        service.assert_not_called()

    def test_mock_login_is_disabled_in_production(self):
        with patch("main.get_settings", return_value=SimpleNamespace(app_env="production")), \
             patch("main.get_connection") as connection:
            response = self.client.post("/auth/login", json={"user_id": 1})
        self.assertEqual(response.status_code, 403)
        connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
