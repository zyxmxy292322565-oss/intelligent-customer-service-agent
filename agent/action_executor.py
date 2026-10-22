from agent.confirmation_store import (
    get_pending_action,
    update_pending_action_execution,
    claim_pending_action_execution
)

from services.after_sale_service import (
    create_after_sale_service
)
from services.policy_execution_service import (
    existing_after_sale_for_key, validate_policy_before_create,
)
from services.after_sale_operation_service import (
    OperationResultUncertain, execute_after_sale_operation, get_operation_receipt,
)

from agent.session_store import (
    clear_current_confirmation
)
import logging
import pymysql
from services.task_service import task_execution_lifecycle
from core.logging import log_extra

logger = logging.getLogger("agent.action_executor")

@task_execution_lifecycle
def execute_confirmed_action(
    confirmation_id: str,
    current_user_id: int,
    trace_id: str | None = None
):

    """
    执行已经经过用户确认的写操作。

    支持创建售后，以及已创建售后的取消和类型变更。
    """


    logger.info("action execution started", extra=log_extra(
        trace_id=trace_id, confirmation_id=confirmation_id
    ))


    # =====================================================
    # 1. 查询待执行操作
    # =====================================================

    action = get_pending_action(
        confirmation_id,
        current_user_id
    )

    if action is not None:
        logger.info("action loaded status=%s", action.get("status"), extra=log_extra(
            trace_id=trace_id, task_id=action.get("task_id"), confirmation_id=confirmation_id
        ))


    if action is None:

        return {

            "success": False,

            "code": "ACTION_NOT_FOUND",

            "message": "待执行操作不存在"

        }



    # =====================================================
    # 2. Ownership Check
    # =====================================================

    if action["user_id"] != current_user_id:

        return {

            "success": False,

            "code": "ACTION_NOT_FOUND",

            "message": "待执行操作不存在"

        }



    # =====================================================
    # 3. 重复执行保护
    # =====================================================

    if action["status"] == "executed":

        return {

            "success": False,

            "code": "ACTION_ALREADY_EXECUTED",

            "message": "该操作已经执行完成"

        }



    if action["status"] == "executing":

        # A prior worker may have committed the post-application mutation and
        # lost its connection before Action finalization.  The receipt, not the
        # current sale state, is the only admissible recovery proof.
        if action["action_type"] in {"cancel_after_sale", "change_after_sale_type"}:
            try:
                receipt = get_operation_receipt(
                    confirmation_id, current_user_id, action["action_type"], action["preview"],
                )
            except (OperationResultUncertain, ValueError):
                receipt = None
            except Exception:
                receipt = None
            if receipt is not None:
                updated = update_pending_action_execution(confirmation_id, "executed", receipt)
                if updated is not None:
                    clear_current_confirmation(current_user_id, confirmation_id)
                    return {
                        "success": True, "code": "ACTION_EXECUTED",
                        "message": "写操作执行成功",
                        "data": {"confirmation_id": confirmation_id, "status": updated["status"], "result": receipt},
                    }

        return {

            "success": False,

            "code": "ACTION_EXECUTING",

            "message": "该操作正在执行中"

        }



    # =====================================================
    # 4. 必须已经确认
    # =====================================================

    if action["status"] != "confirmed":

        return {

            "success": False,

            "code": "ACTION_NOT_CONFIRMED",

            "message": "该操作尚未确认，不能执行"

        }



    # =====================================================
    # 5. 检查支持的操作类型
    # =====================================================

    if action["action_type"] not in {
        "create_after_sale", "cancel_after_sale", "change_after_sale_type"
    }:

        return {

            "success": False,

            "code": "UNSUPPORTED_ACTION",

            "message": "当前操作类型暂不支持"

        }


    policy_validation = None
    if action["action_type"] == "create_after_sale":
        preview = action.get("preview") or {}
        required = ("order_no", "order_item_id", "after_sale_type", "reason")
        if not isinstance(preview, dict) or any(not preview.get(key) for key in required):
            return {"success": False, "code": "PREVIEW_RECHECK_REQUIRED",
                    "message": "售后预览信息不完整，请重新发起申请。"}
        try:
            # A previous insert may have committed before the response was lost.
            # In that case the same idempotency key must replay, not reapply policy.
            already_created = existing_after_sale_for_key(confirmation_id, current_user_id)
            if already_created is None:
                snapshot = preview.get("policy_snapshot")
                policy_reason = preview.get("policy_reason_text")
                if (not isinstance(snapshot, dict) or snapshot.get("snapshot_version") != 2
                        or not isinstance(policy_reason, str) or not policy_reason.strip()):
                    return {"success": False, "code": "PREVIEW_RECHECK_REQUIRED",
                            "message": "历史预览缺少完整原因依据，请重新发起申请。"}
                gate = validate_policy_before_create(
                    user_id=current_user_id, order_no=preview["order_no"],
                    order_item_id=preview["order_item_id"],
                    after_sale_type=preview["after_sale_type"], reason=policy_reason,
                    snapshot=snapshot, require_snapshot=True,
                )
                if not gate["success"]:
                    return gate
                policy_validation = gate["validation"]
        except pymysql.err.OperationalError:
            return {"success": False, "code": "DATABASE_RETRYABLE",
                    "message": "订单服务暂时不可用，请稍后重试。"}
        except Exception:
            logger.exception("pre-claim policy validation failed", extra=log_extra(
                trace_id=trace_id, task_id=action.get("task_id"), confirmation_id=confirmation_id
            ))
            return {"success": False, "code": "POLICY_UNAVAILABLE",
                    "message": "暂时无法核验订单和政策，请稍后重试。"}



    # =====================================================
    # 6. confirmed -> executing
    # Atomic Claim
    # =====================================================

    claimed = claim_pending_action_execution(
        confirmation_id
    )


    if not claimed:

        latest_action = get_pending_action(
            confirmation_id,
            current_user_id
        )


        latest_status = (

            latest_action["status"]

            if latest_action

            else None

        )


        return {

            "success": False,

            "code": "ACTION_ALREADY_CLAIMED",

            "message": "该操作正在执行或已经处理",

            "status": latest_status

        }


    logger.info("action claimed", extra=log_extra(
        trace_id=trace_id, task_id=action.get("task_id"), confirmation_id=confirmation_id
    ))



    preview = action["preview"]



    try:


        # =================================================
        # 7. 调用业务 Service
        # =================================================

        if action["action_type"] == "create_after_sale":
            service_result = create_after_sale_service(
                user_id=current_user_id,
                order_no=preview["order_no"],
                order_item_id=preview["order_item_id"],
                after_sale_type=preview["after_sale_type"],
                reason=preview["reason"],
                idempotency_key=confirmation_id,
                _policy_validation=policy_validation,
                _policy_reason_text=preview["policy_reason_text"] if policy_validation else None,
            )
        else:
            service_result = execute_after_sale_operation(
                user_id=current_user_id,
                conversation_id=action["conversation_id"],
                action_type=action["action_type"],
                preview=preview,
                confirmation_id=confirmation_id,
            )



        # =================================================
        # 8. Service业务失败
        # =================================================

        if service_result["success"] is False:
            update_pending_action_execution(
                confirmation_id=confirmation_id,
                status="failed",
                execution_result=service_result
            )
            clear_current_confirmation(
                user_id=current_user_id,
                confirmation_id=confirmation_id
            )

            return {

                "success": False,

                "code": "ACTION_EXECUTION_FAILED",

                "message": "写操作执行失败",

                "data": {

                    "confirmation_id": confirmation_id,

                    "service_result": service_result

                }

            }



        # =================================================
        # 9. executing -> executed
        # =================================================

        updated_action = (

            update_pending_action_execution(
                confirmation_id=confirmation_id,
                status="executed",
                execution_result=service_result
            )
        )

        # 清理当前用户确认上下文
        clear_current_confirmation(
            user_id=current_user_id,
            confirmation_id=confirmation_id
        )


        logger.info("action executed", extra=log_extra(
            trace_id=trace_id, task_id=action.get("task_id"), confirmation_id=confirmation_id
        ))



        return {

            "success": True,

            "code": "ACTION_EXECUTED",

            "message": "写操作执行成功",

            "data": {

                "confirmation_id": confirmation_id,

                "status": updated_action["status"],

                "result": service_result

            }

        }




    except Exception as exc:

        # Post-application operations use a receipt committed in the same
        # transaction as the sale mutation.  A connection failure after that
        # commit is therefore safely replayable; without that evidence leave
        # the already-claimed Action executing for later investigation.
        if action["action_type"] in {"cancel_after_sale", "change_after_sale_type"}:
            try:
                receipt = get_operation_receipt(
                    confirmation_id, current_user_id, action["action_type"], action["preview"],
                )
            except (OperationResultUncertain, ValueError):
                receipt = None
            except Exception:
                receipt = None
            if receipt is None:
                logger.exception("post-application operation result uncertain", extra=log_extra(
                    trace_id=trace_id, task_id=action.get("task_id"), confirmation_id=confirmation_id,
                ))
                return {
                    "success": False,
                    "code": "ACTION_RESULT_UNCERTAIN",
                    "message": "操作结果正在核查，请勿重复提交。",
                    "data": {"confirmation_id": confirmation_id},
                }
            try:
                update_pending_action_execution(confirmation_id, "executed", receipt)
            except Exception:
                logger.exception("receipt replay could not finalize action", extra=log_extra(
                    trace_id=trace_id, task_id=action.get("task_id"), confirmation_id=confirmation_id,
                ))
            return {
                "success": True,
                "code": "ACTION_EXECUTED",
                "message": "写操作执行成功",
                "data": {"confirmation_id": confirmation_id, "result": receipt},
            }

        # An exception can occur after the business transaction committed.
        # Never turn an uncertain create into failed: the original key is the
        # only admissible proof, and no new business call is made here.
        try:
            existing = existing_after_sale_for_key(confirmation_id, current_user_id)
            if (existing is not None
                    and existing['order_item_id'] == preview['order_item_id']
                    and existing['type'] == preview['after_sale_type']):
                result = {'success': True, 'code': 'IDEMPOTENT_REPLAY', 'data': existing}
                updated = update_pending_action_execution(confirmation_id, 'executed', result)
                if updated is not None:
                    return {'success': True, 'code': 'ACTION_EXECUTED',
                            'message': '售后申请已创建', 'data': {'result': result}}
        except Exception:
            logger.exception('create result finalization requires recovery', extra=log_extra(
                task_id=action.get('task_id'), confirmation_id=confirmation_id))
        return {'success': False, 'code': 'ACTION_RESULT_UNCERTAIN',
                'message': '操作结果正在核查，请勿重复提交。',
                'data': {'confirmation_id': confirmation_id}}
