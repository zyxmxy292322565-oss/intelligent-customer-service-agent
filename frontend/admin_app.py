"""Independent customer-service console; all case data comes from protected APIs."""

import streamlit as st

from api.client import APIError, BackendAPI
from api.realtime import ConversationSocket


STATUS_LABELS = {"waiting": "待接入", "assigned": "处理中", "completed": "已完成"}
SALE_STATUS_LABELS = {
    "pending": "处理中", "processing": "处理中", "approved": "已通过",
    "rejected": "未通过", "completed": "已完成", "cancelled": "已取消",
}
POLICY_CATEGORY_LABELS = {
    "return_policy": "退货政策",
    "exchange_policy": "换货政策",
    "warranty_policy": "保修政策",
    "product_policy": "商品专项政策",
    "general_policy": "通用政策",
}
PRODUCT_SCOPE_LABELS = {
    "general": "通用",
    "phone": "手机",
    "earphone": "耳机",
    "tablet": "平板",
    "computer": "电脑",
}


st.set_page_config(page_title="人工客服工作台", page_icon="🎧", layout="wide")
st.title("人工客服工作台")

if not st.session_state.get("admin_key"):
    st.caption("请输入单独配置的客服工作台凭证。普通用户登录 Token 不能访问此页面的数据。")
    with st.form("admin_login"):
        key = st.text_input("客服工作台凭证", type="password")
        submit = st.form_submit_button("进入工作台")
    if submit:
        try:
            BackendAPI(admin_key=key).admin_handoff_tasks()
        except APIError:
            st.error("凭证无效，或客服工作台尚未配置。")
        else:
            st.session_state.admin_key = key
            st.rerun()
    st.stop()

with st.sidebar:
    if st.button("退出工作台"):
        live = st.session_state.pop("admin_conversation_socket", None)
        if live is not None:
            live.close()
        st.session_state.pop("admin_key", None)
        st.rerun()


@st.fragment(run_every="1s")
def _live_updates():
    live = st.session_state.get("admin_conversation_socket")
    if live is not None and any(event.get("type") in {"message", "handoff_status"}
                                for event in live.drain()):
        st.rerun(scope="app")

api = BackendAPI(admin_key=st.session_state.admin_key)


def _render_policy_management() -> None:
    st.header("知识库管理")
    st.caption("上传或删除政策文档会刷新 Policy RAG 检索索引；不会执行任何售后业务操作。")

    with st.form("policy_document_upload", clear_on_submit=True):
        uploaded_file = st.file_uploader("政策文件", type=["pdf", "txt"])
        left, middle, right = st.columns(3)
        with left:
            category = st.selectbox(
                "文件分类", list(POLICY_CATEGORY_LABELS), format_func=POLICY_CATEGORY_LABELS.get,
            )
        with middle:
            product_scope = st.selectbox(
                "适用商品", list(PRODUCT_SCOPE_LABELS), format_func=PRODUCT_SCOPE_LABELS.get,
            )
        with right:
            version = st.text_input("版本号", placeholder="例如：2026.10", max_chars=64)
        submit = st.form_submit_button("上传并更新知识库")

    if submit:
        if uploaded_file is None:
            st.warning("请选择 PDF 或 TXT 格式的政策文件。")
        elif not version.strip():
            st.warning("请填写政策版本号。")
        else:
            try:
                result = api.admin_policy_upload(
                    uploaded_file.name,
                    uploaded_file.getvalue(),
                    uploaded_file.type,
                    category,
                    product_scope,
                    version.strip(),
                )
                data = result.get("data") or {}
                st.success("政策文档已上传，知识库索引已更新。")
                st.caption(
                    f"文本块数量：{data.get('chunk_count', 0)}；"
                    f"已清理问答缓存：{data.get('deleted_cache_count', 0)} 条"
                )
            except APIError as exc:
                st.error(f"政策文档上传失败：{exc}")

    st.subheader("已上传政策文档")
    try:
        documents = (api.admin_policy_documents().get("data") or {}).get("documents") or []
    except APIError as exc:
        st.error(f"文档列表读取失败：{exc}")
        return

    if not documents:
        st.info("当前知识库还没有政策文档。")
        return

    st.dataframe([{
        "文件名": row.get("file_name") or "未命名文件",
        "分类": POLICY_CATEGORY_LABELS.get(row.get("category"), row.get("category") or "通用政策"),
        "适用商品": PRODUCT_SCOPE_LABELS.get(row.get("product_scope"), row.get("product_scope") or "通用"),
        "版本": row.get("version") or "v1",
        "文本块数量": row.get("chunk_count") or 0,
        "上传时间": str(row.get("created_at") or "").replace("T", " "),
    } for row in documents], hide_index=True, width="stretch")

    by_id = {row.get("id"): row for row in documents if row.get("id") is not None}
    document_id = st.selectbox(
        "选择要删除的政策文档",
        list(by_id),
        format_func=lambda item: f"#{item} · {by_id[item].get('file_name') or '未命名文件'}",
    )
    if st.button("删除文档", type="secondary", key="request_policy_delete"):
        st.session_state.policy_delete_pending_id = document_id

    pending_id = st.session_state.get("policy_delete_pending_id")
    if pending_id in by_id:
        document = by_id[pending_id]
        st.warning(
            f"确认删除《{document.get('file_name') or '未命名文件'}》？"
            "系统会删除其文本块并重建 Policy RAG 索引。"
        )
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            confirmed = st.button("确认删除", type="primary", key="confirm_policy_delete")
        with cancel_col:
            cancelled = st.button("取消", key="cancel_policy_delete")
        if cancelled:
            st.session_state.pop("policy_delete_pending_id", None)
            st.rerun()
        if confirmed:
            try:
                result = api.admin_policy_delete(pending_id)
                data = result.get("data") or {}
                st.session_state.pop("policy_delete_pending_id", None)
                st.success(
                    "政策文档已删除，知识库索引已重建。"
                    f"已清理问答缓存：{data.get('deleted_cache_count', 0)} 条"
                )
                st.rerun()
            except APIError as exc:
                st.error(f"删除政策文档失败：{exc}")


workspace = st.radio("工作台模块", ["人工客服", "知识库管理"], horizontal=True)
if workspace == "知识库管理":
    _render_policy_management()
    st.stop()

status = st.selectbox("工单状态", list(STATUS_LABELS), format_func=STATUS_LABELS.get)
if status != "assigned":
    previous = st.session_state.pop("admin_conversation_socket", None)
    if previous is not None:
        previous.close()
try:
    tasks = (api.admin_handoff_tasks(status).get("data") or {}).get("tasks") or []
except APIError:
    st.error("工单列表读取失败，请检查凭证或稍后重试。")
    st.stop()

if not tasks:
    st.info("当前没有该状态的人工请求。")
    st.stop()

st.subheader("人工请求")
st.dataframe([{
    "编号": row["id"],
    "请求时间": str(row.get("created_at") or "").replace("T", " "),
    "用户": row.get("username") or f"用户 {row['user_id']}",
    "问题": row.get("reason") or "",
    "状态": STATUS_LABELS.get(row.get("status"), "待核实"),
} for row in tasks], hide_index=True, width="stretch")

selected = st.selectbox("查看请求", [row["id"] for row in tasks],
                        format_func=lambda task_id: f"请求 #{task_id}")
if st.button("刷新会话"):
    st.rerun()
try:
    detail = (api.admin_handoff_detail(selected).get("data") or {})
except APIError:
    st.error("请求详情读取失败，请稍后重试。")
    st.stop()

handoff = detail.get("handoff") or {}
live = st.session_state.get("admin_conversation_socket")
if live is not None and (live.conversation_id != handoff.get("conversation_id") or
                         handoff.get("status") != "assigned"):
    live.close()
    st.session_state.pop("admin_conversation_socket", None)
    live = None
if handoff.get("status") == "assigned" and live is None:
    st.session_state.admin_conversation_socket = ConversationSocket(
        api.base_url, handoff["conversation_id"], admin_key=st.session_state.admin_key,
        after_id=max((row.get("id") or 0 for row in detail.get("messages") or []), default=0),
    )
_live_updates()
st.subheader("请求详情")
st.write(f"用户：{handoff.get('username') or handoff.get('user_id') or '未知'}")
st.write(f"会话：{handoff.get('conversation_title') or '未命名会话'}")
st.write(f"转接原因：{handoff.get('reason') or '未填写'}")
st.write(f"当前状态：{STATUS_LABELS.get(handoff.get('status'), '待核实')}")

if handoff.get("status") == "waiting" and st.button("接入用户"):
    try:
        api.admin_handoff_accept(selected)
        st.rerun()
    except APIError:
        st.warning("请求已被其他客服处理，或当前无法接入，请刷新后重试。")
if handoff.get("status") == "assigned" and st.button("完成处理"):
    try:
        api.admin_handoff_complete(selected)
        st.rerun()
    except APIError:
        st.warning("请求状态已变化，或当前无法完成，请刷新后重试。")

if handoff.get("status") == "assigned":
    with st.form(f"reply_{selected}"):
        reply = st.text_area("发送给用户", max_chars=2000)
        send = st.form_submit_button("发送回复")
    if send:
        try:
            api.admin_handoff_message(selected, reply)
            st.rerun()
        except APIError:
            st.warning("回复发送失败；请检查内容或工单状态后重试。")

st.subheader("会话历史")
for message in detail.get("messages") or []:
    original_role = message.get("role")
    role = "user" if original_role == "user" else "assistant"
    with st.chat_message(role):
        if original_role == "system":
            st.caption("系统消息")
        elif original_role == "human_agent":
            st.caption("客服")
        elif original_role == "assistant":
            st.caption("AI")
        st.write(message.get("content") or "")

st.subheader("关联售后")
sales = detail.get("after_sales") or []
if not sales:
    st.caption("当前会话暂无关联售后。")
for sale in sales:
    with st.container(border=True):
        st.write(f"商品：{sale.get('product_name') or '未提供'}")
        st.write(f"申请编号：{sale.get('after_sale_no') or '未提供'}")
        st.write(f"状态：{SALE_STATUS_LABELS.get(sale.get('status'), '待核实')}")

st.subheader("Agent 处理过程")
for task in detail.get("agent_tasks") or []:
    st.write(f"任务 #{task['id']} · {task.get('goal') or ''} · {task.get('status') or ''}")
with st.expander("查看步骤与执行结果"):
    st.dataframe(detail.get("agent_steps") or [], hide_index=True, width="stretch")
    st.dataframe(detail.get("agent_results") or [], hide_index=True, width="stretch")
