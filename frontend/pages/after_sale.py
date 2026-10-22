import streamlit as st
from decimal import Decimal, InvalidOperation

from api.client import APIError, BackendAPI


STATUS_LABELS = {
    "pending": "处理中",
    "confirmed": "已确认",
    "executed": "已完成",
    "failed": "处理失败",
    "cancelled": "已取消",
    "processing": "处理中",
    "approved": "已通过",
    "rejected": "未通过",
    "completed": "已完成",
}

STATUS_HELP = {
    "pending": "你的申请正在审核，请耐心等待。",
    "processing": "你的申请正在审核，请耐心等待。",
    "completed": "售后流程已经完成。",
    "executed": "售后流程已经完成。",
    "failed": "处理失败，请联系客服了解原因。",
    "cancelled": "这笔申请已取消。",
}


def format_refund_amount(amount) -> str:
    if amount is None:
        return "不适用"
    try:
        return f"¥{Decimal(str(amount)):,.2f}"
    except (InvalidOperation, ValueError):
        return "待确认"


def _open_cancel_preview(after_sale_no: str):
    # Streamlit widget keys must be changed in the callback, before rendering.
    st.session_state.prefill_chat_message = f"取消售后申请 {after_sale_no}"
    st.session_state.page = "对话"


def render():
    st.title("售后记录")
    try:
        result = BackendAPI(st.session_state.token).after_sales(st.session_state.user_id)
    except APIError:
        st.error("售后记录暂时无法读取，请稍后重试。")
        return
    if not result.get("success"):
        st.warning(result.get("message") or "查询失败")
        return
    records = (result.get("data") or {}).get("after_sales") or []
    if not records:
        st.info("暂无售后记录")
        return
    for index, record in enumerate(records):
        with st.container(border=True):
            st.write(f"商品：{record.get('product_name') or '未提供'}")
            st.write(f"申请编号：{record.get('after_sale_no') or '待生成'}")
            st.write(f"申请状态：{STATUS_LABELS.get(record.get('status'), '状态待更新')}")
            st.write(f"退款金额：{format_refund_amount(record.get('refund_amount'))}")
            created_at = str(record.get("created_at") or "时间未知").replace("T", " ")
            st.write(f"申请时间：{created_at}")
            if record.get("type"):
                sale_types = {"refund": "仅退款", "return_refund": "退货退款", "exchange": "换货", "repair": "维修"}
                st.write(f"申请类型：{sale_types.get(record['type'], '售后服务')}")
            if record.get("status") in STATUS_HELP:
                st.caption(STATUS_HELP[record["status"]])
            if record.get("status") == "pending":
                st.button(
                    "撤销售后", key=f"cancel_sale_{index}",
                    on_click=_open_cancel_preview, args=(record["after_sale_no"],),
                )
                if st.button("修改售后类型", key=f"change_sale_{index}"):
                    st.info("请在原会话输入“改成维修”或“改成换货”，核对预览后再确认。")
            elif record.get("status") == "executed":
                st.caption("申请已经进入处理流程，请联系客服。")
            elif record.get("status") in {"processing", "approved", "completed"}:
                st.caption("如需修改或撤销，请联系客服。")
