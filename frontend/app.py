import streamlit as st

from api.client import APIError, BackendAPI
from pages import after_sale, chat, trace


st.set_page_config(page_title="智能消费售后 Agent", page_icon="🛍️", layout="wide")

if not st.session_state.get("token"):
    st.title("智能消费售后与订单服务 Agent")
    st.caption("开发演示登录：项目目前以用户 ID 模拟登录，生产环境需要真实身份系统。")
    with st.form("login"):
        user_id = st.number_input("用户 ID", min_value=1, step=1, value=1)
        submitted = st.form_submit_button("登录")
    if submitted:
        try:
            st.session_state.token = BackendAPI().login(int(user_id))
            st.session_state.user_id = int(user_id)
            st.rerun()
        except APIError as exc:
            st.error(str(exc))
    st.stop()

if "conversation_id" not in st.session_state:
    try:
        api = BackendAPI(st.session_state.token)
        conversations = (api.conversations().get("data") or {}).get("conversations") or []
        conversation = conversations[0] if conversations else api.new_conversation()["data"]
        st.session_state.conversation_id = conversation["id"]
        st.session_state.conversation_options = conversations or [conversation]
    except APIError:
        st.error("会话暂时无法读取，请稍后重试。")
        st.stop()

with st.sidebar:
    st.write(f"当前用户：{st.session_state.user_id}")
    page = st.radio("页面", ["对话", "Trace", "售后记录"], key="page")
    if st.button("退出登录"):
        live = st.session_state.pop("conversation_socket", None)
        if live is not None:
            live.close()
        for key in ("token", "user_id", "messages", "messages_conversation_id", "conversation_id", "conversation_options", "page", "handoff_notice", "pending_agent_user_id"):
            st.session_state.pop(key, None)
        st.rerun()

{"对话": chat.render, "Trace": trace.render, "售后记录": after_sale.render}[page]()
