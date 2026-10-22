from agent.context_resolver import (
    resolve_order_item_by_product
)

from schemas.agent_schema import (
    CreateAfterSalePreview
)

from agent.session_store import (
    save_current_confirmation
)

from agent.confirmation_store import (
    create_pending_action
)
from agent.step_trace import trace_step
from agent.conversation_scope import current_scope
from services.policy_check_service import check_after_sale_policy
from services.policy_execution_service import build_policy_snapshot, active_after_sale_for_preview
import logging
from core.logging import log_extra

logger = logging.getLogger("agent.write_workflow")


def run_write_workflow(
    user_text,
    current_user_id,
    input_analysis,
    trace_id=None,
    task_id=None
):

    logger.info("write workflow started", extra=log_extra(trace_id=trace_id, task_id=task_id))



    intent = input_analysis.intent

    params = vars(
        input_analysis.params
    )

    logger.debug("write intent=%s param_fields=%s", intent, sorted(params),
                 extra=log_extra(trace_id=trace_id, task_id=task_id))







    if intent != "create_after_sale":

        return {

            "success": False,

            "code":
                "UNSUPPORTED_WRITE_INTENT",

            "message":
                "当前写操作暂未支持"

        }



    after_sale_type = params.get(
        "after_sale_type"
    )

    reason = params.get(
        "reason"
    )

    product_name = params.get(
        "product_name"
    )



    if after_sale_type is None:

        return {

            "success": False,

            "code":
                "AFTER_SALE_TYPE_REQUIRED",

            "message":
                "请说明售后类型"

        }



    if reason is None:

        return {

            "success": False,

            "code":
                "AFTER_SALE_REASON_REQUIRED",

            "message":
                "请说明申请售后的原因"

        }



    if product_name is None:

        return {

            "success": False,

            "code":
                "PRODUCT_NAME_REQUIRED",

            "message":
                "请说明需要申请售后的商品"

        }

    # =====================================================
    # 5. Context Resolution（上下文解析）
    # =====================================================

    with trace_step(task_id, trace_id, "resolve_order", {
        "user_id": current_user_id, "product_name": product_name
    }) as step:
        resolved_result = resolve_order_item_by_product(
            user_id=current_user_id,
            product_name=product_name,
            trace_id=trace_id
        )
        step.set_result(resolved_result)


    if resolved_result["success"] is False:

        return resolved_result


    return build_create_after_sale_pending_action(
        current_user_id=current_user_id,
        params=params,
        resolved_context=resolved_result["data"],
        trace_id=trace_id,
        task_id=task_id,
    )

def build_create_after_sale_pending_action(
    current_user_id: int,
    params: dict,
    resolved_context: dict,
    trace_id: str | None = None,
    task_id: int | None = None,
) -> dict:

    if task_id is None:
        scope = current_scope()
        task_id = scope.task_id if scope is not None else None

    required_values = {
        "order_no":
            resolved_context.get("order_no"),

        "order_item_id":
            resolved_context.get("order_item_id"),

        "product_name":
            resolved_context.get("product_name"),

        "after_sale_type":
            params.get("after_sale_type"),

        "reason":
            params.get("reason")
    }

    missing_fields = [
        field_name
        for field_name, value
        in required_values.items()
        if value is None
    ]

    if missing_fields:

        return {
            "success": False,
            "code": "PREVIEW_DATA_INCOMPLETE",
            "message": (
                "生成操作预览所需数据不完整"
            ),
            "data": {
                "missing_fields":
                    missing_fields
            }
        }

    # The order and item fields come exclusively from the DB-backed resolver.
    # Missing ownership/category/date fields must never be inferred from text.
    required_order_fields = (
        "user_id", "order_id", "order_no", "order_item_id", "order_status",
    )
    if any(resolved_context.get(field) is None for field in required_order_fields):
        return {
            "success": False,
            "code": "POLICY_CONTEXT_INCOMPLETE",
            "message": "订单信息不足，无法完成自动政策核验，请联系人工客服。",
        }

    if resolved_context["user_id"] != current_user_id:
        return {"success": False, "code": "ORDER_NOT_FOUND", "message": "当前用户没有找到该订单。"}
    existing = active_after_sale_for_preview(
        current_user_id, resolved_context["order_no"], resolved_context["order_item_id"]
    )
    if existing:
        if existing.get("ownership_error"):
            return {"success": False, "code": "ORDER_NOT_FOUND", "message": "当前用户没有找到该订单。"}
        return {"success": False, "code": "AFTER_SALE_ALREADY_EXISTS",
                "message": "该商品已有进行中的售后申请，建议查看现有售后进度。",
                "data": {"after_sale_no": existing.get("after_sale_no")}}

    policy_reason = params.get("original_reason_text") or required_values["reason"]

    with trace_step(task_id, trace_id, "policy_check", {
        "order_no": resolved_context["order_no"],
        "order_item_id": resolved_context["order_item_id"],
        "category": resolved_context.get("category"),
    }) as step:
        policy_result = check_after_sale_policy(
            user_id=current_user_id,
            order={
                "id": resolved_context["order_id"],
                "user_id": resolved_context["user_id"],
                "status": resolved_context["order_status"],
                "created_at": resolved_context.get("created_at"),
                "paid_at": resolved_context.get("paid_at"),
                "delivered_at": resolved_context.get("delivered_at"),
            },
            order_item={
                "id": resolved_context["order_item_id"],
                "order_id": resolved_context["order_id"],
                "product_name": resolved_context["product_name"],
                "category": resolved_context.get("category"),
            },
            after_sale_type=required_values["after_sale_type"],
            reason=policy_reason,
        )
        step.set_result({
            "decision": policy_result["decision"],
            "policy_type": policy_result.get("policy_type"),
            "policy_sources": policy_result.get("sources", []),
            "policy_versions": policy_result.get("policy_versions", {}),
            "days_since_delivery": policy_result.get("days_since_delivery"),
            **({"success": False, "message": "政策服务不可用"}
               if policy_result["decision"] == "POLICY_UNAVAILABLE" else {}),
        })

    decision = policy_result["decision"]
    if decision != "ELIGIBLE_TO_APPLY":
        messages = {
            "INELIGIBLE": policy_result["policy_message"],
            "NEEDS_REVIEW": (
                f"{policy_result['policy_message']} 当前商品的售后条件需要进一步核实，"
                "建议联系人工客服。"
            ),
            "POLICY_UNAVAILABLE": (
                "当前暂时无法完成自动政策核验，可以稍后重试或联系人工客服。"
            ),
        }
        return {
            "success": False,
            "code": decision,
            "message": messages.get(decision, "政策核验未完成，请联系人工客服。"),
            "data": {
                "policy_decision": decision,
                "sources": policy_result.get("sources", []),
            },
        }

    policy_preview = {
        key: policy_result.get(key)
        for key in (
            "decision", "policy_type", "days_since_delivery", "policy_deadline_days",
            "policy_message", "requires_review", "sources", "policy_versions",
            "policy_file_hashes", "evidence",
        )
    }

    with trace_step(task_id, trace_id, "build_preview", {
        "order_no": required_values["order_no"],
        "policy_decision": decision,
    }) as step:
        preview = CreateAfterSalePreview(
            user_id=current_user_id,
            order_no=required_values["order_no"],
            order_item_id=required_values["order_item_id"],
            product_name=required_values["product_name"],
            after_sale_type=required_values["after_sale_type"],
            reason=required_values["reason"],
            policy_reason_text=policy_reason,
            policy=policy_preview,
            policy_snapshot=build_policy_snapshot(
                policy_result,
                order_id=resolved_context["order_id"],
                order_item_id=resolved_context["order_item_id"],
                order_status=resolved_context["order_status"],
                delivered_at=resolved_context.get("delivered_at"),
                category=resolved_context.get("category"),
                product_name=resolved_context["product_name"],
                after_sale_type=required_values["after_sale_type"],
                reason=policy_reason,
                snapshot_version=2,
            ),
        )
        step.set_result({"order_no": preview.order_no, "policy_decision": decision})

    with trace_step(task_id, trace_id, "create_pending_action", {
        "order_no": preview.order_no, "policy_decision": decision,
    }) as step:
        pending_action = create_pending_action(
            user_id=current_user_id,
            action_type="create_after_sale",
            preview=preview.model_dump(),
            trace_id=trace_id,
            task_id=task_id,
        )
        step.set_result({
            "confirmation_id": pending_action["confirmation_id"],
            "status": pending_action["status"],
        })
    save_current_confirmation(
        user_id=current_user_id,
        confirmation_id=pending_action["confirmation_id"]
    )

    return {
        "success": True,
        "code":
            "ACTION_PREVIEW_READY",
        "message": (
            "售后申请信息已经准备完成，"
            "请确认后再提交。"
        ),
        "requires_confirmation":
            True,
        "data": {
            "confirmation_id":
                pending_action[
                    "confirmation_id"
                ],

            "preview":
                preview.model_dump()
        }
    }
