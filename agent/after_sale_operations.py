"""Small explicit command path for post-application actions; no router changes."""

import re
from services.task_service import task_execution_lifecycle

from agent.confirmation_store import create_pending_action
from agent.conversation_scope import current_scope, task_scope
from agent.session_store import save_current_confirmation
from agent.step_trace import trace_step
from services.after_sale_operation_service import (
    list_conversation_after_sales,
    list_owned_after_sales_by_no,
    list_owned_pending_after_sales,
    list_owned_pending_after_sales_by_product,
    list_owned_pending_after_sales_by_normalized_product,
)
from services.task_service import (
    create_agent_task, new_agent_execution_id, update_agent_task_context,
)


TYPE_WORDS = (("退货退款", "return_refund"), ("仅退款", "refund"),
              ("维修", "repair"), ("换货", "exchange"), ("退款", "refund"))
TYPE_MAP = dict(TYPE_WORDS)


def detect_after_sale_operation(message: str) -> tuple[str, str | None] | None:
    """Match only explicit *existing application* commands, never a new sale request."""
    compact = re.sub(r"\s+", "", message)
    if (re.search(r"(?:取消|撤销).*(?:售后|申请)|不(?:想)?要.*售后", compact)
            or any(word in compact for word in ("取消售后", "撤销售后", "取消我的售后", "不想退了", "取消申请", "不要了", "不想要了"))):
        return "cancel_after_sale", None
    if (any(word in compact for word in ("改成", "改为", "换成", "换维修", "修改售后类型"))
            or re.search(r"修改.*(?:售后|申请)|(?:改|换)(?:换货|维修|退款)", compact)):
        target = re.search(
            r"(?:改成|改为|换成|类型为|改|换)(退货退款|仅退款|换货|维修|退款)",
            compact,
        )
        return "change_after_sale_type", TYPE_MAP[target.group(1)] if target else None
    return None


def _cancel_product_name(message: str) -> str | None:
    compact = re.sub(r"\s+", "", message)
    match = re.search(r"(?:取消|撤销)(?:我的|这个|这笔)?(.*?)(?:售后(?:申请)?|申请)", compact)
    if not match:
        return None
    name = match.group(1).strip("的：:，,。 ")
    return name or None


def _change_product_name(message: str) -> str | None:
    compact = re.sub(r"\s+", "", message)
    match = re.search(r"(?:修改|把|我的)(?:我的|这个|这笔)?(.*?)(?:售后|申请)", compact)
    if not match:
        return None
    name = match.group(1).strip("的：:，,。 ")
    return name or None


@task_execution_lifecycle
def prepare_after_sale_operation(
    message: str, current_user_id: int, trace_id: str,
    operation: tuple[str, str | None],
) -> dict:
    scope = current_scope()
    if scope is None or scope.user_id != current_user_id:
        raise ValueError("Post-application action requires an owned conversation")
    action_type, new_type = operation
    execution_id = new_agent_execution_id()
    task_id = create_agent_task(
        current_user_id, "agent", message, action_type, execution_id,
    )["task_id"]
    with task_scope(task_id, execution_id):
        try:
            if action_type == "change_after_sale_type" and new_type is None:
                update_agent_task_context(task_id, "completed", "completed", execution_id)
                return {"success": False, "code": "AFTER_SALE_CHANGE_TYPE_REQUIRED",
                        "message": "请说明想改成退货退款、换货还是维修，并附上商品名称或申请编号。"}
            with trace_step(task_id, trace_id, "resolve_after_sale", {"action_type": action_type}) as step:
                match = re.search(r"AS[0-9A-F]{16}", message.upper())
                product_name = (_cancel_product_name(message) if action_type == "cancel_after_sale"
                                else _change_product_name(message))
                if action_type == "cancel_after_sale" and match:
                    sales = list_owned_after_sales_by_no(current_user_id, match.group())
                elif action_type == "cancel_after_sale" and product_name:
                    sales = list_owned_pending_after_sales_by_product(current_user_id, product_name)
                elif action_type == "change_after_sale_type" and match:
                    sales = list_owned_after_sales_by_no(current_user_id, match.group())
                elif action_type == "change_after_sale_type" and product_name:
                    sales = list_owned_pending_after_sales_by_normalized_product(current_user_id, product_name)
                elif action_type == "change_after_sale_type":
                    sales = list_owned_pending_after_sales(current_user_id)
                else:
                    sales = list_conversation_after_sales(current_user_id, scope.conversation_id)
                    if len(sales) > 1:
                        named = [sale for sale in sales if sale["product_name"] in message]
                        if named:
                            sales = named
                step.set_result({"matched_count": len(sales)})

            if not sales:
                result = {"success": False, "code": "AFTER_SALE_NOT_FOUND",
                          "message": "没有找到符合条件的售后申请，请核对申请编号或商品名称。"}
            elif len(sales) > 1:
                message = ("找到多个售后申请，请提供商品名称或申请编号。"
                           if action_type == "change_after_sale_type"
                           else "找到多笔售后申请，请提供申请编号以便确认。")
                result = {"success": False, "code": "AFTER_SALE_SELECTION_REQUIRED",
                          "message": message}
            else:
                sale = sales[0]
                if sale["status"] == "cancelled":
                    result = {"success": False, "code": "AFTER_SALE_ALREADY_CANCELLED",
                              "message": "这笔售后申请已经取消，无需重复操作。"}
                elif sale["status"] != "pending":
                    result = {"success": False, "code": "AFTER_SALE_NOT_PENDING",
                              "message": "这笔申请已进入处理流程，无法自行取消或修改，请联系客服。"}
                elif action_type == "change_after_sale_type" and sale["type"] == new_type:
                    result = {"success": False, "code": "AFTER_SALE_TYPE_UNCHANGED",
                              "message": "这笔申请已经是该售后类型，无需重复修改。"}
                else:
                    preview = {
                        "operation": action_type,
                        "after_sale_no": sale["after_sale_no"],
                        "after_sale_id": sale["id"],
                        "product_name": sale["product_name"],
                        "current_status": sale["status"],
                    }
                    if action_type == "cancel_after_sale":
                        preview["cancel_reason"] = message
                    else:
                        preview.update({"old_type": sale["type"], "new_type": new_type})
                    with trace_step(task_id, trace_id, "build_preview", preview) as step:
                        step.set_result(preview)
                    with trace_step(task_id, trace_id, "create_pending_action", preview) as step:
                        action = create_pending_action(current_user_id, action_type, preview, trace_id, task_id)
                        step.set_result({"confirmation_id": action["confirmation_id"]})
                    save_current_confirmation(current_user_id, action["confirmation_id"])
                    update_agent_task_context(
                        task_id, "waiting_confirm", "waiting_confirm", execution_id,
                    )
                    return {"success": True, "code": "ACTION_PREVIEW_READY",
                            "message": "请确认售后变更信息。",
                            "data": {"confirmation_id": action["confirmation_id"], "preview": preview}}
            update_agent_task_context(task_id, "completed", "completed", execution_id)
            return result
        except Exception:
            update_agent_task_context(task_id, "failed", "failed", execution_id)
            raise
