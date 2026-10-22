import streamlit as st

from api.client import APIError, BackendAPI
from api.realtime import ConversationSocket
from components.result import render_result


AFTER_SALE_TYPES = (None, "退货退款", "换货", "维修")


def _scroll_to_latest() -> None:
    # The newest turn is rendered first; locate it after the fragment is updated.
    st.iframe(
        """<script>
        const scrollToLatest = () => {
          try {
            const anchor = window.parent.document.getElementById('agent-chat-latest');
            if (anchor) anchor.scrollIntoView({behavior: 'smooth', block: 'start'});
          } catch (_) { /* Keep chat usable if the browser blocks iframe access. */ }
        };
        window.requestAnimationFrame(() =>
          window.requestAnimationFrame(scrollToLatest));
        setTimeout(scrollToLatest, 150);
        </script>""",
        width=1,
        height=1,
    )


def _scroll_token(conversation_id: int, messages: list[dict]) -> tuple:
    # A periodic fragment rerun without a new message must not interrupt reading.
    latest = messages[-1]
    result = latest.get("result") or {}
    confirmation = latest.get("confirmation_result") or {}
    return (
        conversation_id, len(messages), latest.get("id"), latest.get("role"),
        latest.get("text"), result.get("message"), confirmation.get("code"),
    )


def _display_turns(messages: list[dict]) -> list[list[tuple[int, dict]]]:
    """Group chronological messages by user request, then show newest turn first."""
    turns: list[list[tuple[int, dict]]] = []
    for index, entry in enumerate(messages):
        if entry["role"] == "user" or not turns:
            turns.append([])
        turns[-1].append((index, entry))
    return turns[::-1]


def _send_chat(messages: list[dict], message: str) -> None:
    live = st.session_state.get("conversation_socket")
    if live is not None and live.handoff_assigned:
        if not live.send_message(message):
            st.warning("与人工客服的连接暂时中断，正在重连，请稍后重试。")
        return
    messages.append({"role": "user", "text": message})
    try:
        response = BackendAPI(st.session_state.token).chat(
            message, st.session_state.conversation_id
        )
    except APIError as exc:
        response = {
            "success": False,
            "code": {"timeout": "REQUEST_TIMEOUT", "connection": "BACKEND_UNAVAILABLE"}.get(exc.category, "REQUEST_FAILED"),
            "message": {
                "timeout": "请求等待超时，请稍后重试。",
                "connection": "暂时无法连接服务，请稍后重试。",
            }.get(exc.category, "服务暂时不可用，请稍后重试。"),
        }
    messages.append({"role": "assistant", "result": response})


def _load_conversation() -> list[dict] | None:
    api = BackendAPI(st.session_state.token)
    try:
        if "conversation_id" not in st.session_state:
            conversations = (api.conversations().get("data") or {}).get("conversations") or []
            conversation = conversations[0] if conversations else api.new_conversation()["data"]
            st.session_state.conversation_id = conversation["id"]
            st.session_state.conversation_options = conversations or [conversation]
        if st.session_state.get("messages_conversation_id") != st.session_state.conversation_id:
            rows = (api.conversation_messages(st.session_state.conversation_id).get("data") or {}).get("messages") or []
            st.session_state.messages = [
                {"id": row.get("id"), "role": "user", "text": row["content"]} if row["role"] == "user" else
                {"id": row.get("id"), "role": "human_agent", "text": row["content"]} if row["role"] == "human_agent" else
                {"id": row.get("id"), "role": "assistant", "result": {
                    "success": True, "code": "HISTORY", "message": row["content"]
                }}
                for row in rows if row["role"] in {"user", "assistant", "human_agent"}
            ]
            st.session_state.messages_conversation_id = st.session_state.conversation_id
            st.session_state.chat_scroll_force = True
    except APIError:
        st.error("聊天记录暂时无法读取，请稍后重试。")
        return None
    return st.session_state.messages


def _ensure_socket(messages: list[dict]) -> ConversationSocket:
    current = st.session_state.get("conversation_socket")
    if current is not None and current.conversation_id != st.session_state.conversation_id:
        current.close()
        current = None
    if current is None:
        current = ConversationSocket(
            BackendAPI(st.session_state.token).base_url,
            st.session_state.conversation_id,
            token=st.session_state.token,
            after_id=max((entry.get("id") or 0 for entry in messages), default=0),
        )
        st.session_state.conversation_socket = current
    return current


def _consume_live(messages: list[dict], live: ConversationSocket) -> None:
    for event in live.drain():
        if event.get("type") == "handoff_status":
            st.session_state.handoff_notice = (
                "人工客服已接入" if event.get("status") == "assigned" else "人工服务已结束"
            )
            continue
        if event.get("type") != "message":
            continue
        row = event.get("message") or {}
        message_id, role = row.get("id"), row.get("role")
        if not isinstance(message_id, int) or role not in {"user", "assistant", "human_agent"}:
            continue
        if any(entry.get("id") == message_id for entry in messages):
            continue
        # Match only our own optimistic /agent/chat pair; another tab's AI reply
        # must not overwrite the active preview or an earlier request error.
        optimistic = None
        if role == "user":
            optimistic = next((entry for entry in messages if entry.get("id") is None and
                               entry["role"] == "user" and
                               entry.get("text") == row.get("content")), None)
            if optimistic is not None:
                st.session_state.pending_agent_user_id = message_id
        elif role == "assistant":
            pending_user_id = st.session_state.get("pending_agent_user_id")
            pending_user_index = (next((index for index, entry in enumerate(messages)
                                        if entry.get("id") == pending_user_id), None)
                                  if pending_user_id is not None else None)
            if pending_user_index is not None:
                optimistic = next((entry for entry in messages[pending_user_index + 1:]
                                   if entry.get("id") is None and entry["role"] == "assistant"
                                   and entry.get("result", {}).get("code") not in {"REQUEST_FAILED", "REQUEST_TIMEOUT", "BACKEND_UNAVAILABLE"}), None)
            st.session_state.pop("pending_agent_user_id", None)
        if optimistic is not None:
            optimistic["id"] = message_id
            continue
        entry = {"id": message_id, "role": role}
        if role == "assistant":
            entry["result"] = {"success": True, "code": "HISTORY", "message": row.get("content", "")}
        else:
            entry["text"] = row.get("content", "")
        messages.append(entry)
    messages.sort(key=lambda entry: entry.get("id") if entry.get("id") is not None else float("inf"))


def _request_handoff():
    try:
        BackendAPI(st.session_state.token).handoff(st.session_state.conversation_id)
        st.session_state.handoff_notice = "已为您转接人工客服，请稍候"
    except APIError:
        st.session_state.handoff_notice = "暂时无法转接，请稍后重试。"


def _activate_conversation(conversation_id: int) -> None:
    live = st.session_state.pop("conversation_socket", None)
    if live is not None:
        live.close()
    st.session_state.conversation_id = conversation_id
    st.session_state.messages = []
    st.session_state.messages_conversation_id = None
    st.session_state.chat_scroll_force = True
    st.session_state.pop("pending_agent_user_id", None)
    st.session_state.pop("handoff_notice", None)


def _select_conversation() -> None:
    selected_id = st.session_state.conversation_selector
    if selected_id != st.session_state.conversation_id:
        _activate_conversation(selected_id)


@st.fragment(run_every="1s")
def render():
    st.title("Agent 对话")
    messages = _load_conversation()
    if messages is None:
        return
    conversations = st.session_state.get("conversation_options") or []
    conversation_ids = [conversation["id"] for conversation in conversations]
    if st.session_state.conversation_id not in conversation_ids:
        conversations.insert(0, {"id": st.session_state.conversation_id, "title": "新会话"})
        conversation_ids.insert(0, st.session_state.conversation_id)
    if st.session_state.get("conversation_selector") != st.session_state.conversation_id:
        st.session_state.conversation_selector = st.session_state.conversation_id
    selected_id = st.selectbox(
        "会话", conversation_ids, key="conversation_selector", on_change=_select_conversation,
        format_func=lambda item_id: (
            f"{next((item.get('title') or '新会话' for item in conversations if item['id'] == item_id), '新会话')}"
            f" · #{item_id}"
        ),
    )
    if selected_id != st.session_state.conversation_id:
        _activate_conversation(selected_id)
        st.rerun()
    left, right = st.columns(2)
    with left:
        if st.button("新建会话"):
            try:
                conversation = BackendAPI(st.session_state.token).new_conversation()["data"]
                if all(item["id"] != conversation["id"] for item in conversations):
                    conversations.insert(0, conversation)
                _activate_conversation(conversation["id"])
                st.rerun()
            except APIError:
                st.error("新会话创建失败，请稍后重试。")
    with right:
        if st.button("转人工客服"):
            _request_handoff()
    if st.button("刷新消息"):
        st.session_state.messages_conversation_id = None
        st.session_state.chat_scroll_force = True
        st.rerun()
    if st.session_state.get("handoff_notice"):
        st.info(st.session_state.handoff_notice)
    live = _ensure_socket(messages)
    _consume_live(messages, live)
    question = st.chat_input("请输入订单或售后问题")
    prefilled = st.session_state.pop("prefill_chat_message", None)
    if prefilled:
        _send_chat(messages, prefilled)
    if question:
        _send_chat(messages, question)

    if messages:
        st.markdown('<div id="agent-chat-latest"></div>', unsafe_allow_html=True)

    for index, entry in (item for turn in _display_turns(messages) for item in turn):
        with st.chat_message("user" if entry["role"] == "user" else "assistant"):
            if entry["role"] == "user":
                st.caption("用户")
                st.write(entry["text"])
                continue
            if entry["role"] == "human_agent":
                st.caption("客服")
                st.write(entry["text"])
                continue
            st.caption("AI")
            result = entry["result"]
            render_result(result)
            if (
                result.get("code") == "AFTER_SALE_TYPE_REQUIRED"
                and index == len(messages) - 1
                and not entry.get("type_selected")
            ):
                selected = st.selectbox(
                    "选择售后类型",
                    AFTER_SALE_TYPES,
                    format_func=lambda value: value or "请选择售后类型",
                    key=f"after_sale_type_{index}",
                )
                if selected is not None:
                    entry["type_selected"] = selected
                    _send_chat(messages, f"我选择{selected}")
                    st.rerun()
            data = result.get("data") or {}
            confirmation_id = data.get("confirmation_id") if isinstance(data, dict) else None
            if confirmation_id and "confirmation_result" not in entry:
                preview = data.get("preview") or {}
                label = "确认取消" if preview.get("operation") == "cancel_after_sale" else (
                    "确认修改" if preview.get("operation") == "change_after_sale_type" else "确认并执行"
                )
                if st.button(label, key=f"confirm_{index}"):
                    try:
                        entry["confirmation_result"] = BackendAPI(st.session_state.token).confirm(
                            confirmation_id, st.session_state.conversation_id
                        )
                    except APIError:
                        entry["confirmation_result"] = {
                            "success": False, "code": "REQUEST_FAILED", "message": "确认请求未完成，请稍后重试。"
                        }
                    failure = entry["confirmation_result"]
                    service_result = (failure.get("data") or {}).get("service_result") or {}
                    if service_result.get("code") == "AFTER_SALE_ALREADY_EXISTS":
                        preview = data.get("preview") or {}
                        try:
                            records = (BackendAPI(st.session_state.token).after_sales(
                                st.session_state.user_id
                            ).get("data") or {}).get("after_sales") or []
                            entry["existing_after_sale"] = next((record for record in records if (
                                record.get("order_item_id") == preview.get("order_item_id")
                            )), None)
                        except APIError:
                            entry["existing_after_sale"] = None
                    st.rerun()
            if "confirmation_result" in entry:
                render_result(entry["confirmation_result"], entry.get("existing_after_sale"))
                service_result = (entry["confirmation_result"].get("data") or {}).get("service_result") or {}
                if service_result.get("code") == "AFTER_SALE_ALREADY_EXISTS":
                    if st.button("查看售后详情", key=f"sale_detail_{index}"):
                        st.session_state.page = "售后记录"
                        st.rerun()

    if messages:
        token = _scroll_token(st.session_state.conversation_id, messages)
        if st.session_state.pop("chat_scroll_force", False) or st.session_state.get("chat_scroll_token") != token:
            _scroll_to_latest()
        st.session_state.chat_scroll_token = token
