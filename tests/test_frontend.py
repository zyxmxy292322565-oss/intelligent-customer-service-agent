"""Presentation-layer checks; no backend or database connection is required."""

import sys
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from streamlit.testing.v1 import AppTest


FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
sys.path.insert(0, str(FRONTEND_DIR))

from api.client import APIError, BackendAPI  # noqa: E402
from pages.after_sale import STATUS_LABELS, format_refund_amount  # noqa: E402
from pages.trace import build_timeline  # noqa: E402
from components.result import render_result  # noqa: E402
from pages import chat as chat_page  # noqa: E402


class FrontendClientTests(unittest.TestCase):
    @patch("api.client.httpx.request")
    def test_login_chat_confirm_after_sales_and_trace_carry_jwt(self, request):
        request.side_effect = [
            httpx.Response(200, json={"access_token": "token-1"}, request=httpx.Request("POST", "http://backend/auth/login")),
            httpx.Response(200, json={"success": True, "code": "ACTION_PREVIEW_READY", "data": {"confirmation_id": "CONF_1"}}, request=httpx.Request("POST", "http://backend/agent/chat")),
            httpx.Response(200, json={"success": True, "code": "ACTION_EXECUTED"}, request=httpx.Request("POST", "http://backend/agent/confirm")),
            httpx.Response(200, json={"success": True, "data": {"after_sales": []}}, request=httpx.Request("GET", "http://backend/users/1/after-sales")),
            httpx.Response(200, json={"success": True, "data": {"tasks": [{"id": 42}]}}, request=httpx.Request("GET", "http://backend/agent/tasks")),
            httpx.Response(200, json={"success": True, "data": {"steps": []}}, request=httpx.Request("GET", "http://backend/agent/trace/42")),
        ]
        client = BackendAPI(base_url="http://backend")
        self.assertEqual(client.login(1), "token-1")
        self.assertEqual(client.chat("我要退无线键盘")["code"], "ACTION_PREVIEW_READY")
        self.assertEqual(client.confirm("CONF_1")["code"], "ACTION_EXECUTED")
        client.after_sales(1)
        self.assertEqual(client.tasks()["data"]["tasks"][0]["id"], 42)
        client.trace(42)

        self.assertNotIn("Authorization", request.call_args_list[0].kwargs["headers"])
        for call in request.call_args_list[1:]:
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer token-1")
            self.assertFalse(call.kwargs["trust_env"])
        self.assertEqual(request.call_args_list[2].kwargs["json"], {"confirmation_id": "CONF_1"})
        self.assertEqual(request.call_args_list[5].args[:2], ("GET", "http://backend/agent/trace/42"))

    @patch("api.client.httpx.request")
    def test_http_error_is_reported_to_page(self, request):
        request.return_value = httpx.Response(404, json={"detail": "用户不存在"}, request=httpx.Request("POST", "http://backend/agent/chat"))
        with self.assertLogs("api.client", level="WARNING") as logs, self.assertRaises(APIError) as error:
            BackendAPI("bad-token", "http://backend").chat("测试")
        self.assertIn("HTTP 404", str(error.exception))
        self.assertIn("用户不存在", str(error.exception))
        self.assertIn("url=http://backend/agent/chat status=404", logs.output[0])
        self.assertIn('body={"detail":"用户不存在"}', logs.output[0])

    @patch.dict("os.environ", {"BACKEND_URL": ""})
    def test_local_default_backend_address(self):
        self.assertEqual(BackendAPI().base_url, "http://localhost:8000")

    @patch("api.client.httpx.request")
    def test_conversation_and_handoff_requests_use_jwt(self, request):
        request.return_value = httpx.Response(200, json={"success": True, "data": {}}, request=httpx.Request("GET", "http://backend/agent/conversations"))
        api = BackendAPI("token-1", "http://backend")
        api.conversations()
        api.new_conversation()
        api.conversation_messages(42)
        api.handoff(42)
        for call in request.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer token-1")
        self.assertEqual(request.call_args_list[2].args[:2], ("GET", "http://backend/agent/conversations/42/messages"))
        self.assertEqual(request.call_args_list[3].kwargs["json"], {"reason": "用户请求人工客服"})

    @patch("api.client.httpx.request")
    def test_human_reply_uses_separate_admin_credential(self, request):
        request.return_value = httpx.Response(200, json={"success": True, "data": {"role": "human_agent"}},
                                             request=httpx.Request("POST", "http://backend/admin/handoff/9/message"))
        result = BackendAPI(base_url="http://backend", admin_key="admin-only").admin_handoff_message(9, "您好")
        self.assertEqual(result["data"]["role"], "human_agent")
        self.assertEqual(request.call_args.kwargs["headers"], {"X-Admin-Key": "admin-only"})
        self.assertEqual(request.call_args.kwargs["json"], {"content": "您好"})

    @patch("api.client.httpx.request")
    def test_policy_management_calls_backend_with_admin_key_only(self, request):
        request.side_effect = [
            httpx.Response(200, json={"success": True, "data": {"documents": []}},
                           request=httpx.Request("GET", "http://backend/admin/policy/documents")),
            httpx.Response(200, json={"success": True, "data": {"chunk_count": 2}},
                           request=httpx.Request("POST", "http://backend/admin/policy/documents")),
            httpx.Response(200, json={"success": True, "data": {"deleted_chunk_count": 2}},
                           request=httpx.Request("DELETE", "http://backend/admin/policy/documents/8")),
        ]
        api = BackendAPI(base_url="http://backend", admin_key="admin-only")
        api.admin_policy_documents()
        api.admin_policy_upload("policy.txt", b"policy", "text/plain", "return_policy", "earphone", "2026.10")
        api.admin_policy_delete(8)

        for call in request.call_args_list:
            self.assertEqual(call.kwargs["headers"], {"X-Admin-Key": "admin-only"})
        self.assertEqual(request.call_args_list[1].kwargs["data"], {
            "category": "return_policy", "product_scope": "earphone", "version": "2026.10",
        })
        self.assertEqual(request.call_args_list[2].args[:2], ("DELETE", "http://backend/admin/policy/documents/8"))


class FrontendPageTests(unittest.TestCase):
    class _FakeSocket:
        def __init__(self, base_url, conversation_id, **kwargs):
            self.conversation_id = conversation_id
            self.handoff_assigned = False
            self.events = []

        def drain(self):
            result, self.events = self.events, []
            return result

        def send_message(self, content):
            return True

        def close(self):
            pass

    @patch("api.client.BackendAPI.conversation_messages", return_value={"success": True, "data": {"messages": [
        {"role": "user", "content": "我的耳机有问题"},
        {"role": "assistant", "content": "我来帮您处理"},
        {"role": "human_agent", "content": "您好，请提供订单号"},
    ]}})
    def test_customer_history_labels_ai_and_human_agent(self, history):
        app = self._app()
        app.session_state["token"] = "user-token"
        app.session_state["user_id"] = 1
        app.run()
        content = " ".join(item.value for item in app.markdown)
        self.assertIn("我的耳机有问题", content)
        self.assertIn("我来帮您处理", content)
        self.assertIn("您好，请提供订单号", content)
        self.assertTrue(any(item.value == "AI" for item in app.caption))
        self.assertTrue(any(item.value == "客服" for item in app.caption))
        self.assertEqual([entry["role"] for entry in app.session_state["messages"]],
                         ["user", "assistant", "human_agent"])

    @patch("api.client.BackendAPI.conversation_messages", return_value={"success": True, "data": {"messages": [
        {"id": 1, "role": "user", "content": "问题A"},
        {"id": 2, "role": "assistant", "content": "回答A"},
        {"id": 3, "role": "user", "content": "问题B"},
        {"id": 4, "role": "assistant", "content": "回答B"},
        {"id": 5, "role": "human_agent", "content": "客服补充B"},
    ]}})
    def test_history_turns_newest_first_but_messages_remain_chronological(self, history):
        app = self._app()
        app.session_state["token"] = "user-token"
        app.session_state["user_id"] = 1
        app.run()
        rendered = " ".join(item.value for item in app.markdown)
        self.assertEqual(
            [rendered.index(text) for text in ("问题B", "回答B", "客服补充B", "问题A", "回答A")],
            sorted(rendered.index(text) for text in ("问题B", "回答B", "客服补充B", "问题A", "回答A")),
        )
        self.assertEqual([entry["id"] for entry in app.session_state["messages"]], [1, 2, 3, 4, 5])
        history.assert_called_once_with(1)
        self.assertEqual(len(app.exception), 0)

    def test_customer_receives_human_reply_without_manual_refresh(self):
        app = self._app()
        app.session_state["token"] = "user-token"
        app.session_state["user_id"] = 1
        app.run()
        live = app.session_state["conversation_socket"]
        live.events.append({"type": "message", "message": {
            "id": 7, "conversation_id": 1, "role": "human_agent", "content": "我来帮您处理",
        }})
        app.run()
        self.assertTrue(any(item.value == "客服" for item in app.caption))
        self.assertIn("我来帮您处理", " ".join(item.value for item in app.markdown))
        app.run()
        self.assertEqual(sum(entry["role"] == "human_agent" for entry in app.session_state["messages"]), 1)

    def test_chat_locates_latest_turn_after_new_messages_and_refresh_but_not_idle_rerun(self):
        history = {"success": True, "data": {"messages": [
            {"id": 1, "role": "user", "content": "旧问题"},
            {"id": 2, "role": "assistant", "content": "旧回答"},
        ]}}
        with patch("api.client.BackendAPI.conversation_messages", return_value=history), \
             patch("pages.chat._scroll_to_latest") as scroll, \
             patch("pages.chat.BackendAPI.chat", return_value={
                 "success": True, "code": "POLICY_RESULT_READY", "data": {"answer": "新回答"},
             }):
            app = self._app()
            app.session_state["token"] = "user-token"
            app.session_state["user_id"] = 1
            app.run()
            self.assertEqual([entry["id"] for entry in app.session_state["messages"]], [1, 2])
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(scroll.call_count, 1)
            app.run()  # One-second fragment reruns must not steal the scroll position.
            self.assertEqual(scroll.call_count, 1)

            app.chat_input[0].set_value("新问题").run()
            self.assertEqual([entry["role"] for entry in app.session_state["messages"]],
                             ["user", "assistant", "user", "assistant"])
            self.assertEqual(scroll.call_count, 2)

            live = app.session_state["conversation_socket"]
            live.events.extend([
                {"type": "message", "message": {
                    "id": 3, "conversation_id": 1, "role": "user", "content": "新问题",
                }},
                {"type": "message", "message": {
                    "id": 4, "conversation_id": 1, "role": "assistant", "content": "新回答",
                }},
                {"type": "message", "message": {
                    "id": 5, "conversation_id": 1, "role": "human_agent", "content": "客服新消息",
                }},
            ])
            app.run()
            self.assertEqual(app.session_state["messages"][-1]["role"], "human_agent")
            self.assertEqual(scroll.call_count, 3)

            next(button for button in app.button if button.label == "刷新消息").click().run()
            self.assertEqual(scroll.call_count, 4)
            self.assertEqual(len(app.exception), 0)

    @patch("pages.chat.st.iframe")
    def test_scroll_component_targets_newest_turn_anchor(self, html):
        chat_page._scroll_to_latest()
        script = html.call_args.args[0]
        self.assertIn("agent-chat-latest", script)
        self.assertIn("block: 'start'", script)
        self.assertIn("scrollIntoView", script)
        self.assertEqual(html.call_args.kwargs["height"], 1)

    def test_live_echo_does_not_duplicate_local_chat_or_erase_preview(self):
        app = self._app()
        app.session_state["token"] = "user-token"
        app.session_state["user_id"] = 1
        app.run()
        messages = app.session_state["messages"]
        messages.append({"role": "user", "text": "我要退耳机"})
        messages.append({"role": "assistant", "result": {
            "success": True, "code": "ACTION_PREVIEW_READY",
            "data": {"confirmation_id": "CONF_TEST", "preview": {"product_name": "耳机"}},
        }})
        live = app.session_state["conversation_socket"]
        live.events.extend([
            {"type": "message", "message": {"id": 8, "role": "user", "content": "我要退耳机"}},
            {"type": "message", "message": {"id": 9, "role": "assistant", "content": "请确认售后申请"}},
        ])
        app.run()
        self.assertEqual(len(app.session_state["messages"]), 2)
        self.assertEqual([entry["id"] for entry in app.session_state["messages"]], [8, 9])
        self.assertEqual(app.session_state["messages"][1]["result"]["code"], "ACTION_PREVIEW_READY")

    @patch("api.client.BackendAPI.admin_handoff_tasks", return_value={"success": True, "data": {"tasks": [
        {"id": 9, "user_id": 1, "username": "客户1", "reason": "转人工", "status": "assigned"},
    ]}})
    @patch("api.client.BackendAPI.admin_handoff_detail", return_value={"success": True, "data": {
        "handoff": {"id": 9, "conversation_id": 1, "username": "客户1", "reason": "转人工", "status": "assigned"},
        "messages": [{"role": "user", "content": "转人工"}],
        "after_sales": [], "agent_tasks": [], "agent_steps": [], "agent_results": [],
    }})
    @patch("api.client.BackendAPI.admin_handoff_message", return_value={"success": True})
    def test_admin_workbench_sends_reply(self, send, detail, tasks):
        app = AppTest.from_file(str(FRONTEND_DIR / "admin_app.py"), default_timeout=10)
        app.session_state["admin_key"] = "test-admin-key"
        app.run()
        app.selectbox[0].set_value("assigned").run()
        app.text_area[0].set_value("您好，请提供订单号。")
        next(button for button in app.button if button.label == "发送回复").click().run()
        send.assert_called_once_with(9, "您好，请提供订单号。")

    @patch("api.client.BackendAPI.admin_handoff_tasks", return_value={"success": True, "data": {"tasks": []}})
    @patch("api.client.BackendAPI.admin_policy_documents", return_value={"success": True, "data": {"documents": [{
        "id": 8, "file_name": "耳机政策.pdf", "category": "product_policy", "product_scope": "earphone",
        "version": "2026.10", "chunk_count": 5, "created_at": "2026-10-13T10:00:00",
    }]}})
    def test_admin_workbench_renders_policy_management_page(self, documents, _tasks):
        app = AppTest.from_file(str(FRONTEND_DIR / "admin_app.py"), default_timeout=10)
        app.session_state["admin_key"] = "test-admin-key"
        app.run()
        app.radio[0].set_value("知识库管理").run()
        self.assertTrue(any(item.value == "知识库管理" for item in app.header))
        self.assertIn("耳机政策.pdf", " ".join(str(item.value) for item in app.dataframe))
        documents.assert_called()

    @patch("components.result.st")
    def test_post_sale_result_uses_customer_facing_language(self, st):
        render_result({"success": True, "code": "ACTION_PREVIEW_READY", "data": {"preview": {
            "operation": "change_after_sale_type", "product_name": "AirPods Pro",
            "after_sale_no": "AS_1", "old_type": "return_refund", "new_type": "repair",
        }}})
        st.write.assert_any_call("原申请：退货退款")
        st.write.assert_any_call("修改为：维修")
        render_result({"success": True, "code": "ACTION_EXECUTED", "data": {"result": {"data": {
            "operation": "cancel_after_sale", "after_sale_no": "AS_1",
        }}}})
        st.success.assert_called_with("售后申请已取消")
        render_result({"success": True, "code": "IDEMPOTENT_REPLAY", "data": {"data": {
            "operation": "change_after_sale_type", "after_sale_no": "AS_1",
        }}})
        st.success.assert_called_with("售后类型已修改")
        render_result({"success": False, "code": "ACTION_EXECUTION_FAILED", "data": {
            "service_result": {"code": "AFTER_SALE_NOT_PENDING"},
        }})
        st.warning.assert_called_with("申请已进入处理流程，无法自行取消或修改，请联系客服。")

    @patch("components.result.st")
    def test_missing_reason_asks_for_reason_instead_of_generic_failure(self, st):
        render_result({"success": False, "code": "AFTER_SALE_REASON_REQUIRED"})
        st.warning.assert_called_once_with("请说明申请售后的原因，例如商品哪里有问题。")

    def setUp(self):
        for name in ("api.realtime.ConversationSocket", "pages.chat.ConversationSocket"):
            socket_patch = patch(name, self._FakeSocket)
            socket_patch.start()
            self.addCleanup(socket_patch.stop)
        for name, value in (
            ("conversations", {"success": True, "data": {"conversations": [{"id": 1}]}}),
            ("new_conversation", {"success": True, "data": {"id": 2}}),
            ("conversation_messages", {"success": True, "data": {"messages": []}}),
        ):
            client_patch = patch(f"api.client.BackendAPI.{name}", return_value=value)
            client_patch.start()
            self.addCleanup(client_patch.stop)

    def _app(self):
        return AppTest.from_file(str(FRONTEND_DIR / "app.py"), default_timeout=10)

    def test_only_app_navigation_is_exposed(self):
        config = tomllib.loads((FRONTEND_DIR / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
        self.assertFalse(config["client"]["showSidebarNavigation"])

    @patch("api.client.BackendAPI.login", return_value="token-1")
    @patch("pages.trace.BackendAPI.tasks", return_value={"success": True, "data": {"tasks": [{"id": 42}]}})
    @patch("pages.trace.BackendAPI.trace", return_value={"success": True, "data": {"steps": [{"task_id": 42}]}})
    @patch("pages.after_sale.BackendAPI.after_sales", return_value={"success": True, "data": {"after_sales": []}})
    def test_page_switch_keeps_login_and_renders_each_page(self, after_sales, trace, tasks, login):
        app = self._app().run()
        app.button[0].click().run()
        login.assert_called_once_with(1)
        for page, title in (("Trace", "服务进度"), ("售后记录", "售后记录"), ("对话", "Agent 对话")):
            app.radio[0].set_value(page).run()
            self.assertEqual(app.session_state["token"], "token-1")
            self.assertEqual(app.session_state["user_id"], 1)
            self.assertTrue(any(item.value == title for item in app.title))
        tasks.assert_called_once_with(1)
        trace.assert_called_once_with(42, 1)
        after_sales.assert_called_once_with(1)

    @patch("api.client.BackendAPI.login", return_value="token-1")
    def test_login_then_chat_and_confirm(self, login):
        with patch("api.client.BackendAPI.chat", return_value={
            "success": True, "code": "ACTION_PREVIEW_READY", "message": "请确认",
            "data": {"confirmation_id": "CONF_1", "preview": {
                "product_name": "AirPods Pro", "after_sale_type": "return_refund", "reason": "质量问题"
            }},
        }) as chat, patch("api.client.BackendAPI.confirm", return_value={
            "success": True, "code": "ACTION_EXECUTED", "message": "执行成功",
            "data": {"result": {"data": {"after_sale_no": "AS_123"}}},
        }) as confirm:
            app = self._app().run()
            app.button[0].click().run()
            self.assertEqual(app.session_state["token"], "token-1")
            login.assert_called_once_with(1)
            app.chat_input[0].set_value("我要退无线键盘").run()
            chat.assert_called_once_with("我要退无线键盘", 1)
            preview_text = " ".join(item.value for item in app.markdown)
            self.assertIn("商品：AirPods Pro", preview_text)
            self.assertIn("申请类型：退货退款", preview_text)
            self.assertIn("原因：质量问题", preview_text)
            self.assertEqual(len(app.json), 0)
            self.assertFalse(any("ACTION_PREVIEW_READY" in item.value for item in app.caption))
            app.button(key="confirm_1").click().run()
            confirm.assert_called_once_with("CONF_1", 1)
            self.assertEqual(app.success[0].value, "售后申请已创建")
            self.assertIn("申请编号：AS_123", " ".join(item.value for item in app.markdown))
            self.assertTrue(any("我们会尽快处理" in item.value for item in app.caption))
            self.assertFalse(any("ACTION_EXECUTED" in item.value for item in app.caption))
            self.assertFalse(any("CONF_1" in item.value for item in app.markdown))

    def test_missing_after_sale_type_selects_and_submits_once(self):
        for selected in ("退货退款", "换货", "维修"):
            with self.subTest(selected=selected), patch("pages.chat.BackendAPI.chat", side_effect=[
                {"success": False, "code": "AFTER_SALE_TYPE_REQUIRED", "message": "请说明售后类型"},
                {"success": True, "code": "ACTION_PREVIEW_READY", "data": {
                    "confirmation_id": "CONF_1", "preview": {
                        "product_name": "AirPods Pro", "after_sale_type": "return_refund", "reason": "质量不好"
                    },
                }},
            ]) as chat:
                app = self._app()
                app.session_state["token"] = "token-1"
                app.session_state["user_id"] = 1
                app.run()
                app.chat_input[0].set_value("我要退AirPods Pro，质量不好").run()
                self.assertEqual(app.selectbox[1].label, "选择售后类型")
                self.assertIn(selected, app.selectbox[1].options)
                app.selectbox[1].set_value(selected).run()
                self.assertEqual(chat.call_args_list[1].args, (f"我选择{selected}", 1))
                self.assertEqual(len(app.session_state["messages"]), 4)
                self.assertIn("商品：AirPods Pro", " ".join(item.value for item in app.markdown))
                app.run()
                self.assertEqual(chat.call_count, 2)

    @patch("pages.trace.BackendAPI.trace", return_value={"success": True, "data": {"steps": [
        {"task_id": 42, "trace_id": "trace-1", "step_name": name,
         "status": status, "latency_ms": 12, "created_at": "2026-10-12"}
        for name, status in (("input_analysis", "SUCCESS"), ("resolve_order", "SUCCESS"),
                             ("build_preview", "SUCCESS"), ("confirm", "SUCCESS"),
                             ("execute", "FAILED"))
    ]}})
    @patch("pages.trace.BackendAPI.tasks", return_value={"success": True, "data": {"tasks": [{"id": 42, "status": "completed"}]}})
    def test_trace_table_displays_steps(self, tasks_mock, trace_mock):
        app = self._app()
        app.session_state["token"] = "token-1"
        app.session_state["user_id"] = 7
        app.run()
        app.radio[0].set_value("Trace").run()
        tasks_mock.assert_called_once_with(1)
        trace_mock.assert_called_once_with(42, 1)
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(app.expander[0].label, "查看详细 Trace")
        self.assertFalse(app.expander[0].proto.expanded)
        timeline_text = " ".join(item.value for item in app.markdown)
        for label in ("理解需求", "查询订单", "生成售后方案", "用户确认", "完成"):
            self.assertIn(label, timeline_text)
        self.assertTrue(any("这次售后申请未创建成功" in item.value for item in app.warning))
        self.assertNotIn("trace-1", timeline_text)

    @patch("pages.chat.BackendAPI.chat", return_value={
        "success": True, "code": "POLICY_RESULT_READY", "message": "请参考政策依据",
        "data": {"answer": "20 天是否可退须结合规则和订单核验", "sources": ["policy.md"], "evidence": [
            {"source": "policy.md", "content": "退货规则", "version": "v1"},
        ]},
    })
    def test_policy_answer_hides_evidence_until_expanded(self, chat):
        app = self._app()
        app.session_state["token"] = "token-1"
        app.session_state["user_id"] = 7
        app.run()
        app.chat_input[0].set_value("AirPods 用了 20 天还能退吗？").run()
        chat.assert_called_once()
        self.assertIn("20 天是否可退须结合规则和订单核验", " ".join(item.value for item in app.markdown))
        self.assertEqual(app.expander[0].label, "查看政策依据")
        self.assertFalse(app.expander[0].proto.expanded)
        self.assertFalse(any("POLICY_RESULT_READY" in item.value for item in app.caption))
        self.assertEqual(len([button for button in app.button if button.label == "确认并执行"]), 0)

    @patch("pages.chat.BackendAPI.chat", side_effect=APIError("http://backend:8000/agent/chat HTTP 503"))
    def test_chat_error_hides_internal_url(self, chat):
        app = self._app()
        app.session_state["token"] = "token-1"
        app.session_state["user_id"] = 7
        app.run()
        app.chat_input[0].set_value("查询政策").run()
        chat.assert_called_once()
        self.assertTrue(any("服务暂时不可用" in item.value for item in app.warning))
        self.assertFalse(any("backend:8000" in item.value for item in app.warning))

    @patch("pages.after_sale.BackendAPI.after_sales", return_value={
        "success": True, "data": {"after_sales": [{
            "product_name": "AirPods Pro", "after_sale_no": "AS_1", "status": "pending", "refund_amount": "99.00",
            "created_at": "2026-10-12",
        }]},
    })
    def test_after_sale_records_are_displayed_from_api(self, after_sales):
        app = self._app()
        app.session_state["token"] = "token-1"
        app.session_state["user_id"] = 7
        app.run()
        app.radio[0].set_value("售后记录").run()
        after_sales.assert_called_once_with(7)
        self.assertEqual(len(app.dataframe), 0)
        card = " ".join(item.value for item in app.markdown)
        self.assertIn("商品：AirPods Pro", card)
        self.assertIn("申请编号：AS_1", card)
        self.assertIn("申请状态：处理中", card)
        self.assertIn("退款金额：¥99.00", card)

    def test_statuses_and_timeline_use_user_facing_values(self):
        self.assertEqual([STATUS_LABELS[value] for value in
                          ("pending", "confirmed", "executed", "failed")],
                         ["处理中", "已确认", "已完成", "处理失败"])
        self.assertEqual(format_refund_amount(None), "不适用")
        self.assertEqual(build_timeline([
            {"step_name": "input_analysis", "status": "SUCCESS"},
            {"step_name": "execute", "status": "FAILED"},
        ])[-1], ("完成", "未完成"))
        self.assertEqual(build_timeline([
            {"step_name": "input_analysis", "status": "SUCCESS"},
        ]), [("理解需求", "已完成")])

    @patch("api.client.BackendAPI.login", return_value="token-1")
    @patch("pages.chat.BackendAPI.chat", side_effect=[
        {"success": True, "code": "POLICY_RESULT_READY", "message": "政策检索成功", "data": {
            "answer": "20天已超过无理由退货期；质量问题可申请检测。",
            "evidence": [{"source": "耳机类商品规则.txt", "content": "质量问题30天内可申请。"}],
        }},
        {"success": False, "code": "AFTER_SALE_TYPE_REQUIRED", "message": "请说明售后类型"},
        {"success": True, "code": "ACTION_PREVIEW_READY", "data": {
            "confirmation_id": "CONF_1",
            "preview": {"product_name": "AirPods Pro", "after_sale_type": "return_refund", "reason": "质量不好"},
        }},
    ])
    @patch("pages.chat.BackendAPI.confirm", return_value={
        "success": True, "code": "ACTION_EXECUTED", "data": {
            "result": {"data": {"after_sale_no": "AS_1"}},
        },
    })
    @patch("pages.trace.BackendAPI.tasks", return_value={
        "success": True, "data": {"tasks": [{"id": 42, "created_at": "2026-10-12T08:00:00"}]},
    })
    @patch("pages.trace.BackendAPI.trace", return_value={
        "success": True, "data": {"steps": [
            {"task_id": 42, "step_name": name, "status": "SUCCESS"}
            for name in ("input_analysis", "resolve_order", "build_preview", "confirm", "execute")
        ]},
    })
    @patch("pages.after_sale.BackendAPI.after_sales", return_value={
        "success": True, "data": {"after_sales": [{
            "product_name": "AirPods Pro", "after_sale_no": "AS_1", "status": "pending",
            "refund_amount": "1399.00", "created_at": "2026-10-12T08:00:00",
        }]},
    })
    def test_full_frontend_journey(self, after_sales, trace, tasks, confirm, chat, login):
        app = self._app().run()
        app.button[0].click().run()
        login.assert_called_once_with(1)

        app.chat_input[0].set_value("AirPods用了20天还能退吗？").run()
        self.assertIn("20天已超过无理由退货期", " ".join(item.value for item in app.markdown))
        self.assertEqual(app.expander[0].label, "查看政策依据")

        app.chat_input[0].set_value("我要退AirPods Pro，质量不好").run()
        app.selectbox[1].set_value("退货退款").run()
        self.assertEqual(chat.call_args_list[2].args, ("我选择退货退款", 1))
        self.assertIn("申请类型：退货退款", " ".join(item.value for item in app.markdown))

        app.button(key="confirm_5").click().run()
        confirm.assert_called_once_with("CONF_1", 1)
        self.assertEqual(app.success[0].value, "售后申请已创建")
        self.assertIn("申请编号：AS_1", " ".join(item.value for item in app.markdown))

        app.radio[0].set_value("Trace").run()
        tasks.assert_called_once_with(1)
        trace.assert_called_once_with(42, 1)
        self.assertIn("完成", " ".join(item.value for item in app.markdown))

        app.radio[0].set_value("售后记录").run()
        after_sales.assert_called_once_with(1)
        self.assertIn("商品：AirPods Pro", " ".join(item.value for item in app.markdown))
        self.assertEqual(app.session_state["token"], "token-1")

    @patch("api.client.BackendAPI.login", return_value="token-1")
    @patch("pages.chat.BackendAPI.chat", return_value={
        "success": True, "code": "POLICY_RESULT_READY", "message": "政策回答",
        "data": {"answer": "超过无理由期限，质量问题仍可申请检测。", "evidence": [{"source": "耳机政策"}]},
    })
    def test_logout_login_restores_persisted_history_and_new_conversation_is_empty(self, chat, login):
        history = {"success": True, "data": {"messages": [
            {"role": "user", "content": "AirPods用了20天还能退吗？"},
            {"role": "assistant", "content": "超过无理由期限，质量问题仍可申请检测。"},
        ]}}
        with patch("api.client.BackendAPI.conversation_messages", side_effect=[
            {"success": True, "data": {"messages": []}}, history,
            {"success": True, "data": {"messages": []}},
        ]):
            app = self._app().run()
            app.button[0].click().run()
            app.chat_input[0].set_value("AirPods用了20天还能退吗？").run()
            chat.assert_called_once_with("AirPods用了20天还能退吗？", 1)
            next(button for button in app.button if button.label == "退出登录").click().run()
            self.assertNotIn("messages", app.session_state)
            app.button[0].click().run()
            content = " ".join(item.value for item in app.markdown)
            self.assertIn("超过无理由期限，质量问题仍可申请检测。", content)
            next(button for button in app.button if button.label == "新建会话").click().run()
            self.assertEqual(app.session_state["conversation_id"], 2)
            self.assertEqual(app.session_state["messages"], [])

    def test_new_conversation_and_switch_keep_messages_isolated(self):
        conversations = {}
        history = {}

        def create_conversation():
            conversation_id = len(conversations) + 1
            conversation = {"id": conversation_id, "title": "新会话"}
            conversations[conversation_id] = conversation
            history[conversation_id] = []
            return {"success": True, "data": conversation}

        def load_messages(conversation_id):
            return {"success": True, "data": {"messages": list(history[conversation_id])}}

        def send_message(message, conversation_id):
            history[conversation_id].extend([
                {"id": 1, "role": "user", "content": message},
                {"id": 2, "role": "assistant", "content": "回答A"},
            ])
            return {"success": True, "code": "POLICY_RESULT_READY", "data": {"answer": "回答A"}}

        with patch("api.client.BackendAPI.conversations", return_value={
                "success": True, "data": {"conversations": []}}), \
             patch("api.client.BackendAPI.new_conversation", side_effect=create_conversation), \
             patch("api.client.BackendAPI.conversation_messages", side_effect=load_messages) as load, \
             patch("pages.chat.BackendAPI.chat", side_effect=send_message) as send:
            app = self._app()
            app.session_state["token"] = "token-1"
            app.session_state["user_id"] = 1
            app.run()
            self.assertEqual(app.session_state["conversation_id"], 1)
            self.assertEqual(app.session_state["messages"], [])

            app.chat_input[0].set_value("问题A").run()
            send.assert_called_once_with("问题A", 1)
            self.assertIn("问题A", " ".join(item.value for item in app.markdown))

            next(button for button in app.button if button.label == "新建会话").click().run()
            self.assertEqual(app.session_state["conversation_id"], 2)
            self.assertEqual(app.session_state["messages_conversation_id"], 2)
            self.assertEqual(app.session_state["messages"], [])
            self.assertNotIn("问题A", " ".join(item.value for item in app.markdown))
            self.assertEqual(app.selectbox[0].value, 2)
            self.assertEqual(len(set(app.selectbox[0].options)), 2)

            app.selectbox[0].set_value(1).run()
            self.assertEqual(app.session_state["conversation_id"], 1)
            self.assertEqual(app.session_state["messages_conversation_id"], 1)
            self.assertEqual([entry["text"] for entry in app.session_state["messages"] if entry["role"] == "user"],
                             ["问题A"])
            self.assertIn("问题A", " ".join(item.value for item in app.markdown))

            app.selectbox[0].set_value(2).run()
            self.assertEqual(app.session_state["conversation_id"], 2)
            self.assertEqual(app.session_state["messages"], [])
            app.selectbox[0].set_value(1).run()
            self.assertEqual(app.session_state["conversation_id"], 1)
            self.assertIn("问题A", " ".join(item.value for item in app.markdown))
            self.assertEqual(load.call_args_list[-1].args, (1,))
            self.assertEqual(len(app.exception), 0)

    @patch("pages.trace.BackendAPI.tasks", return_value={
        "success": True, "data": {"tasks": []},
    })
    def test_new_conversation_trace_does_not_request_old_tasks(self, tasks):
        app = self._app()
        app.session_state["token"] = "token-1"
        app.session_state["user_id"] = 1
        app.run()
        next(button for button in app.button if button.label == "新建会话").click().run()
        app.radio[0].set_value("Trace").run()
        tasks.assert_called_once_with(2)
        self.assertTrue(any("暂无任务记录" in item.value for item in app.info))

    @patch("pages.chat.BackendAPI.chat", return_value={
        "success": True, "code": "ACTION_PREVIEW_READY", "message": "请确认",
        "data": {"confirmation_id": "CONF_1", "preview": {
            "order_item_id": 1, "product_name": "AirPods Pro", "after_sale_type": "repair", "reason": "质量问题",
        }},
    })
    @patch("pages.chat.BackendAPI.confirm", return_value={
        "success": False, "code": "ACTION_EXECUTION_FAILED", "message": "写操作执行失败",
        "data": {"service_result": {"success": False, "code": "AFTER_SALE_ALREADY_EXISTS"}},
    })
    @patch("pages.chat.BackendAPI.after_sales", return_value={
        "success": True, "data": {"after_sales": [{
            "order_item_id": 1, "product_name": "AirPods Pro", "after_sale_no": "AS_1",
            "status": "pending", "type": "return_refund", "created_at": "2026-10-12T08:00:00",
        }]},
    })
    def test_duplicate_sale_shows_existing_application_not_internal_failure(self, sales, confirm, chat):
        app = self._app()
        app.session_state["token"] = "token-1"
        app.session_state["user_id"] = 1
        app.run()
        app.chat_input[0].set_value("我想改维修").run()
        app.button(key="confirm_1").click().run()
        content = " ".join(item.value for item in app.markdown)
        self.assertIn("该商品已有售后申请", " ".join(item.value for item in app.warning))
        self.assertIn("已有申请编号：AS_1", content)
        self.assertIn("当前类型：退货退款", content)
        self.assertNotIn("写操作执行失败", content)
        self.assertTrue(any(button.label == "查看售后详情" for button in app.button))

    @patch("pages.chat.BackendAPI.handoff", return_value={
        "success": True, "data": {"id": 1, "status": "waiting"},
    })
    def test_handoff_button_records_request(self, handoff):
        app = self._app()
        app.session_state["token"] = "token-1"
        app.session_state["user_id"] = 1
        app.run()
        next(button for button in app.button if button.label == "转人工客服").click().run()
        handoff.assert_called_once_with(1)
        self.assertTrue(any("已为您转接人工客服，请稍候" in item.value for item in app.info))

    @patch("api.client.BackendAPI.admin_handoff_tasks", return_value={
        "success": True, "data": {"tasks": [{
            "id": 9, "user_id": 1, "username": "客户1", "reason": "转人工",
            "status": "waiting", "created_at": "2026-10-12 12:00:00",
        }]},
    })
    @patch("api.client.BackendAPI.admin_handoff_detail", return_value={
        "success": True, "data": {
            "handoff": {"id": 9, "username": "客户1", "reason": "转人工", "status": "waiting"},
            "messages": [{"role": "user", "content": "转人工"}],
            "after_sales": [], "agent_tasks": [], "agent_steps": [], "agent_results": [],
        },
    })
    @patch("api.client.BackendAPI.admin_handoff_accept", return_value={"success": True})
    def test_admin_workbench_login_list_detail_and_accept(self, accept, detail, tasks):
        app = AppTest.from_file(str(FRONTEND_DIR / "admin_app.py"), default_timeout=10).run()
        self.assertTrue(any("客服工作台凭证" in item.label for item in app.text_input))
        app.text_input[0].set_value("test-admin-key")
        next(button for button in app.button if button.label == "进入工作台").click().run()
        self.assertEqual(app.session_state["admin_key"], "test-admin-key")
        self.assertTrue(any("转人工" in item.value for item in app.markdown))
        next(button for button in app.button if button.label == "接入用户").click().run()
        accept.assert_called_once_with(9)
        detail.assert_called()
        tasks.assert_called()

    @patch("pages.after_sale.BackendAPI.after_sales", return_value={
        "success": True, "data": {"after_sales": [{
            "product_name": "AirPods Pro", "after_sale_no": "AS_1", "status": "pending",
            "type": "return_refund", "created_at": "2026-10-12",
        }]},
    })
    @patch("pages.chat.BackendAPI.chat", return_value={
        "success": True, "code": "ACTION_PREVIEW_READY", "data": {
            "confirmation_id": "CONF_CANCEL", "preview": {
                "operation": "cancel_after_sale", "product_name": "AirPods Pro",
                "after_sale_no": "AS_1", "current_status": "pending",
                "cancel_reason": "取消售后申请 AS_1",
            },
        },
    })
    def test_pending_sale_cancel_opens_chat_preview(self, chat, sales):
        app = self._app()
        app.session_state["token"] = "token-1"
        app.session_state["user_id"] = 1
        app.run()
        app.radio[0].set_value("售后记录").run()
        self.assertIn("申请类型：退货退款", " ".join(item.value for item in app.markdown))
        next(button for button in app.button if button.label == "撤销售后").click().run()
        self.assertEqual(app.session_state["page"], "对话")
        chat.assert_called_once_with("取消售后申请 AS_1", 1)
        self.assertTrue(any("请确认取消售后" in item.value for item in app.markdown))
        sales.assert_called()


if __name__ == "__main__":
    unittest.main()
