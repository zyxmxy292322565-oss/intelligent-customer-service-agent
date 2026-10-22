"""Presentation-only rendering of backend responses."""

import streamlit as st


ERROR_MESSAGES = {
    "AFTER_SALE_REASON_REQUIRED": "请说明申请售后的原因，例如商品哪里有问题。",
    "PRODUCT_NAME_REQUIRED": "请说明需要申请售后的商品。",
    "AFTER_SALE_ALREADY_EXISTS": "该商品已有售后申请。如需修改售后类型，请先撤销当前申请或联系客服。",
    "PRODUCT_NOT_FOUND": "未找到对应商品，请核对商品名称。",
    "ORDER_NOT_FOUND": "未找到订单，请核对订单信息。",
    "ORDER_NOT_EXIST": "未找到订单，请核对订单信息。",
    "PERMISSION_DENIED": "无权操作这笔订单或售后申请。",
    "SYSTEM_ERROR": "系统繁忙，请稍后重试。",
    "REQUEST_FAILED": "服务暂时不可用，请稍后重试。",
    "REQUEST_TIMEOUT": "请求等待超时，请稍后重试。",
    "BACKEND_UNAVAILABLE": "暂时无法连接服务，请稍后重试。",
    "INPUT_ANALYSIS_TIMEOUT": "理解您的问题时超时，请稍后重试。",
    "INPUT_ANALYSIS_UNAVAILABLE": "暂时无法理解您的问题，请稍后重试。",
    "AFTER_SALE_NOT_FOUND": "当前会话没有找到这笔售后，请切换到原会话或核对申请编号。",
    "AFTER_SALE_ALREADY_CANCELLED": "这笔售后已经取消，无需重复操作。",
    "AFTER_SALE_NOT_PENDING": "申请已进入处理流程，无法自行取消或修改，请联系客服。",
    "AFTER_SALE_TYPE_UNCHANGED": "这笔申请已经是该售后类型。",
    "AFTER_SALE_CHANGE_TYPE_REQUIRED": "请说明想改成退货退款、换货还是维修。",
    "AFTER_SALE_SELECTION_REQUIRED": "当前会话有多笔售后，请提供商品名称或申请编号。",
    "AFTER_SALE_STATE_CONFLICT": "申请状态已变化，请刷新后重试。",
    "ACTION_NOT_FOUND": "没有找到可确认的操作，请返回原会话重试。",
    "POLICY_CONTEXT_INCOMPLETE": "订单信息不足，无法自动核验售后政策，请联系人工客服。",
    "NEEDS_REVIEW": "当前商品的售后条件需要进一步核实，请点击“转人工客服”协助处理。",
    "POLICY_UNAVAILABLE": "当前暂时无法完成自动政策核验，请稍后重试或联系人工客服。",
    "PREVIEW_RECHECK_REQUIRED": "售后政策或订单信息可能已变化，请重新发起申请并核对新的确认方案。",
    "POLICY_VALIDATION_REQUIRED": "需要重新核验售后政策，请重新发起申请。",
    "DATABASE_RETRYABLE": "订单处理繁忙，请稍后重试。",
}


def render_result(result: dict, existing_after_sale: dict | None = None):
    code = result.get("code")
    data = result.get("data") or {}
    if result.get("success") and code in {"ACTION_EXECUTED", "IDEMPOTENT_REPLAY"}:
        execution = data.get("result", data) if isinstance(data, dict) else {}
        details = (execution.get("data") or {}) if isinstance(execution, dict) else {}
        after_sale_no = details.get("after_sale_no") or (
            execution.get("after_sale_no") if isinstance(execution, dict) else None
        )
        operation = details.get("operation")
        success_text = {
            "cancel_after_sale": "售后申请已取消",
            "change_after_sale_type": "售后类型已修改",
        }.get(operation, "售后申请已创建")
        st.success(success_text)
        if after_sale_no:
            st.write(f"申请编号：{after_sale_no}")
        st.caption("可在售后记录查看最新状态。" if operation else "我们会尽快处理。可在售后记录查看进度。")
        return
    if code == "AFTER_SALE_TYPE_REQUIRED":
        st.info("请选择你希望的售后处理方式：")
        return
    if not result.get("success", True):
        service_result = data.get("service_result") if isinstance(data, dict) else None
        business_code = (service_result or {}).get("code") or code
        if business_code == "INELIGIBLE":
            st.warning(result.get("message") or "当前申请路径不符合演示政策，请联系人工客服了解其他方式。")
        else:
            st.warning(ERROR_MESSAGES.get(business_code, "暂时无法处理，请稍后重试。"))
        if business_code == "AFTER_SALE_ALREADY_EXISTS" and existing_after_sale:
            from pages.after_sale import STATUS_LABELS
            sale_types = {"refund": "仅退款", "return_refund": "退货退款", "exchange": "换货", "repair": "维修"}
            st.write(f"商品：{existing_after_sale.get('product_name') or '未提供'}")
            st.write(f"已有申请编号：{existing_after_sale.get('after_sale_no') or '待查询'}")
            st.write(f"当前类型：{sale_types.get(existing_after_sale.get('type'), '售后服务')}")
            st.write(f"当前状态：{STATUS_LABELS.get(existing_after_sale.get('status'), '状态待更新')}")
            st.write(f"申请时间：{str(existing_after_sale.get('created_at') or '时间未知').replace('T', ' ')}")
        return
    if not isinstance(data, dict):
        st.write(result.get("message") or "暂无回答")
        return

    preview = data.get("preview")
    if code == "ACTION_PREVIEW_READY" and isinstance(preview, dict):
        sale_types = {
            "refund": "仅退款",
            "return_refund": "退货退款",
            "exchange": "换货",
            "repair": "维修",
        }
        if preview.get("operation") == "cancel_after_sale":
            st.write("请确认取消售后：")
            st.write(f"商品：{preview.get('product_name') or '未提供'}")
            st.write(f"申请编号：{preview.get('after_sale_no') or '未提供'}")
            st.write("当前状态：处理中")
            st.write(f"取消原因：{preview.get('cancel_reason') or '用户主动取消'}")
            return
        if preview.get("operation") == "change_after_sale_type":
            st.write("请确认修改售后类型：")
            st.write(f"商品：{preview.get('product_name') or '未提供'}")
            st.write(f"申请编号：{preview.get('after_sale_no') or '未提供'}")
            st.write(f"原申请：{sale_types.get(preview.get('old_type'), '售后服务')}")
            st.write(f"修改为：{sale_types.get(preview.get('new_type'), '售后服务')}")
            return
        st.write("请核对售后申请信息：")
        st.write(f"商品：{preview.get('product_name') or '未提供'}")
        st.write(f"订单：{preview.get('order_no') or '未提供'}")
        st.write(f"申请类型：{sale_types.get(preview.get('after_sale_type'), '售后服务')}")
        st.write(f"原因：{preview.get('reason') or '未提供'}")
        policy = preview.get("policy") or {}
        if policy:
            days = policy.get("days_since_delivery")
            st.write(f"签收情况：已签收{days}天" if days is not None else "签收情况：待核实")
            st.write(f"政策核验：{policy.get('policy_message') or '需要进一步核实'}")
            if policy.get("requires_review"):
                st.caption("审核说明：提交申请不代表质量问题已认定或退款已批准，仍需检测与审核。")
            evidence = policy.get("evidence") or []
            if evidence:
                with st.expander("查看政策依据"):
                    for item in evidence:
                        st.caption(
                            f"来源：{item.get('source') or '未知'} · "
                            f"{item.get('version') or '版本待核实'}"
                        )
                        st.write(item.get("content") or "（无摘录）")
        return

    answer = data.get("answer")
    if answer:
        st.write(answer)
        if data.get("evidence"):
            with st.expander("查看政策依据"):
                for item in data["evidence"]:
                    st.caption(f"来源：{item.get('source') or '未知'}")
                    st.write(item.get("content") or "（无摘录）")
        return

    st.write(result.get("message") or "暂无回答")
