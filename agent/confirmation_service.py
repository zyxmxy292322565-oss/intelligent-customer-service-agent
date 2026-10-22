from agent.confirmation_store import (
    get_pending_action,
    update_pending_action_status,
    expire_pending_action_if_due,
    expire_pending_actions as expire_pending_actions_store,
)
from agent.session_store import clear_current_confirmation
import logging
from core.logging import log_extra

logger = logging.getLogger("agent.confirmation_service")

# pending -> confirmed -> executing -> executed/failed
# pending -> cancelled/expired；cancelled、expired 为终态，不允许确认或执行。
def close_pending_action(confirmation_id: str, current_user_id: int, status: str):
    """供取消操作或到期处理调用；仅允许本人的 pending 操作进入终态。"""
    if status not in {"cancelled", "expired"}:
        raise ValueError("只允许取消或过期 pending action")
    action = get_pending_action(confirmation_id, current_user_id)
    if action is None or action["user_id"] != current_user_id:
        return {"success": False, "code": "ACTION_NOT_FOUND", "message": "待确认操作不存在"}
    if action["status"] != "pending":
        return {"success": False, "code": "INVALID_ACTION_STATUS", "message": "当前操作状态不允许取消或过期"}
    updated = update_pending_action_status(confirmation_id, status)
    if updated is None:
        return {"success": False, "code": "ACTION_STATE_CONFLICT", "message": "操作状态已变化"}
    clear_current_confirmation(user_id=current_user_id, confirmation_id=confirmation_id)
    return {"success": True, "code": "ACTION_CLOSED", "message": "操作已关闭", "data": {"confirmation_id": confirmation_id, "status": status}}


def cancel_action(confirmation_id: str, current_user_id: int):
    """仅允许操作所有者取消尚未确认的 Action。"""
    expire_pending_action_if_due(confirmation_id, current_user_id)
    return close_pending_action(confirmation_id, current_user_id, "cancelled")


def expire_pending_actions(max_age_days: int = 7) -> int:
    """供后台定时清理调用，默认过期七天前创建且仍待确认的 Action。"""
    count = expire_pending_actions_store(max_age_days)
    if count:
        logger.info("expired pending actions count=%s", count)
    return count


def _status_result(action):
    status = action["status"]
    if status == "executed":
        if action["execution_result"] is None:
            return {
                "success": False,
                "code": "EXECUTION_RESULT_NOT_FOUND",
                "message": "操作已执行，但执行结果缺失"
            }
        return {
            "success": True,
            "code": "IDEMPOTENT_REPLAY",
            "message": "操作已执行，返回历史结果",
            "data": action["execution_result"]
        }
    if status == "confirmed":
        return {"success": False, "code": "ACTION_ALREADY_CONFIRMED", "message": "该操作已经确认过了"}
    if status == "executing":
        return {"success": False, "code": "ACTION_EXECUTING", "message": "该操作正在执行中"}
    return {"success": False, "code": "INVALID_ACTION_STATUS", "message": "当前操作状态不允许确认"}



def confirm_action(
    confirmation_id: str,
    current_user_id: int,
    trace_id: str | None = None
):

    logger.info("confirmation started", extra=log_extra(
        trace_id=trace_id, confirmation_id=confirmation_id
    ))


    expire_pending_action_if_due(confirmation_id, current_user_id)

    action = get_pending_action(
        confirmation_id,
        current_user_id
    )


    if action is None:

        return {

            "success": False,

            "code": "ACTION_NOT_FOUND",

            "message": "待确认操作不存在"

        }



    if action["user_id"] != current_user_id:

        return {

            "success": False,

            "code": "ACTION_NOT_FOUND",

            "message": "待确认操作不存在"

        }



    if action["status"] != "pending":
        return _status_result(action)



    updated_action = update_pending_action_status(

        confirmation_id=confirmation_id,

        new_status="confirmed"

    )

    if updated_action is None:
        # 并发确认时状态可能已经改变；绝不把已执行/取消的操作改回 confirmed。
        expire_pending_action_if_due(confirmation_id, current_user_id)
        latest_action = get_pending_action(confirmation_id, current_user_id)
        if latest_action is None or latest_action["user_id"] != current_user_id:
            return {"success": False, "code": "ACTION_NOT_FOUND", "message": "待确认操作不存在"}
        if latest_action["status"] != "pending":
            return _status_result(latest_action)
        return {"success": False, "code": "ACTION_CONFIRM_CONFLICT", "message": "确认状态更新失败，请重试"}


    logger.info("confirmation succeeded", extra=log_extra(
        trace_id=trace_id, task_id=action.get("task_id"), confirmation_id=confirmation_id
    ))



    return {

        "success": True,

        "code": "ACTION_CONFIRMED",

        "message": "操作确认成功",

        "data": {

            "confirmation_id": confirmation_id,

            "action_type": updated_action[
                "action_type"
            ],

            "status": updated_action[
                "status"
            ],

            "preview": updated_action[
                "preview"
            ]

        }

    }
