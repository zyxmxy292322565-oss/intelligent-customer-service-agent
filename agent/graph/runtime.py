"""LangGraph chat adapter; business action state remains in MySQL."""

from functools import lru_cache

from agent.state import create_agent_state
from agent.graph.state_graph import build_graph
from agent.policy_response import format_policy_result, POLICY_BUSINESS_RESULTS
from agent.conversation_scope import current_scope, task_scope
from agent.step_trace import trace_step
from agent.intent_router import route_intent
from agent.input_budget import InputAnalysisError
from services.task_service import task_execution_lifecycle
from services.task_service import (
    create_agent_task,
    get_active_agent_task,
    get_task_context,
    update_agent_task_context,
    update_task_context,
    claim_agent_task_execution,
    new_agent_execution_id,
)


_WRITE_INTENTS = {"create_after_sale"}


@lru_cache(maxsize=1)
def _get_graph():
    return build_graph()


@task_execution_lifecycle
def run_graph_agent(user_text: str, current_user_id: int, trace_id: str) -> dict:
    """Attach the optional Graph path to the same conversation-owned task store."""
    scope = current_scope()
    if scope is None:
        return _run_graph_agent_core(user_text, current_user_id, trace_id)
    if scope.user_id != current_user_id:
        raise ValueError("Graph user does not match conversation scope")
    execution_id = new_agent_execution_id()
    active_task = get_active_agent_task(current_user_id)
    if active_task:
        task_id = active_task["id"]
        if not claim_agent_task_execution(
            task_id, current_user_id, scope.conversation_id, execution_id,
        ):
            return {
                "success": False,
                "code": "TASK_EXECUTION_CONFLICT",
                "message": "当前会话任务正在处理中，请稍后重试。",
                "data": {"task_id": task_id},
            }
    else:
        task_id = create_agent_task(
            user_id=current_user_id, task_type="agent", goal=user_text,
            intent="unknown", execution_id=execution_id,
        )["task_id"]
    old_context = get_task_context(task_id, execution_id)
    continuation_intent = None
    continuation_goal = None
    if active_task and active_task["status"] == "waiting_input":
        continuation_intent = old_context.get("_intent")
        if continuation_intent is None and old_context:
            # Backward compatibility for tasks created before _intent was
            # persisted in the JSON task context.
            continuation_goal = active_task["goal"]
        if continuation_intent not in _WRITE_INTENTS:
            continuation_intent = None

    merged_context: dict = {}
    with task_scope(task_id, execution_id):
        try:
            with trace_step(task_id, trace_id, "graph_invoke", {"message_length": len(user_text)}) as step:
                response = _run_graph_agent_core(
                    user_text,
                    current_user_id,
                    trace_id,
                    task_id,
                    old_context,
                    continuation_intent,
                    merged_context,
                    continuation_goal,
                )
                step.set_result({"code": response.get("code"), "success": response.get("success")})
        except InputAnalysisError as exc:
            update_agent_task_context(task_id, "failed", "failed", execution_id)
            return exc.as_result()
        except Exception:
            update_agent_task_context(task_id, "failed", "failed", execution_id)
            raise
        if merged_context:
            update_task_context(
                task_id=task_id, context=merged_context, execution_id=execution_id,
            )
        if response.get("code") == "ACTION_PREVIEW_READY":
            status = "waiting_confirm"
        elif response.get("code") in {
            "PRODUCT_NAME_REQUIRED", "AFTER_SALE_TYPE_REQUIRED", "AFTER_SALE_REASON_REQUIRED"
        }:
            status = "waiting_input"
        elif response.get("code") in POLICY_BUSINESS_RESULTS:
            status = "completed"
        elif response.get("success") is False:
            status = "failed"
        else:
            status = "completed"
        update_agent_task_context(
            task_id,
            "policy_check" if response.get("code") in {"INELIGIBLE", "NEEDS_REVIEW"} else status,
            status,
            execution_id,
        )
        return response


def _run_graph_agent_core(
    user_text: str,
    current_user_id: int,
    trace_id: str,
    task_id: int | str | None = None,
    task_context: dict | None = None,
    continuation_intent: str | None = None,
    context_sink: dict | None = None,
    continuation_goal: str | None = None,
) -> dict:
    state = create_agent_state(
        user_text,
        current_user_id,
        trace_id,
        task_id=task_id,
        task_context=task_context,
        continuation_intent=continuation_intent,
    )
    state["continuation_goal"] = continuation_goal
    result = _get_graph().invoke(
        state,
        config={"configurable": {"thread_id": state["task_id"]}},
    )
    if context_sink is not None:
        context_sink.update(result.get("params") or {})

    error = result.get("error")
    if error:
        return {
            "success": False,
            "code": error.get("code", "AGENT_ERROR"),
            "message": error.get("message", "Agent 处理失败"),
            "data": error.get("data"),
        }

    if result.get("intent") == "query_policy":
        policy_calls = [
            entry for entry in (result.get("tool_results") or [])
            if entry.get("tool_name") == "policy_rag_tool"
        ]
        if not policy_calls:
            return {
                "success": False,
                "code": "POLICY_TOOL_NOT_CALLED",
                "message": "政策检索尚未执行",
                "data": None,
            }
        return format_policy_result(policy_calls[-1]["result"], user_text)

    interrupts = result.get("__interrupt__") or ()
    confirmation_id = result.get("confirmation_id")
    if interrupts and confirmation_id:
        interrupt_value = getattr(interrupts[0], "value", {}) or {}
        return {
            "success": True,
            "code": "ACTION_PREVIEW_READY",
            "message": result.get("final_answer") or "售后申请信息已经准备完成，请确认后再提交。",
            "data": {
                "confirmation_id": confirmation_id,
                "preview": interrupt_value.get("preview"),
            },
        }

    plan = result.get("plan")
    if result.get("intent") == "create_after_sale" and plan and plan.steps:
        first_step = plan.steps[0]
        if first_step.step_type == "clarify":
            missing_codes = {
                "product_name": "PRODUCT_NAME_REQUIRED",
                "after_sale_type": "AFTER_SALE_TYPE_REQUIRED",
                "reason": "AFTER_SALE_REASON_REQUIRED",
            }
            code = next(
                (missing_codes[field] for field in first_step.required_fields if field in missing_codes),
                None,
            )
            if code:
                return {
                    "success": False,
                    "code": code,
                    "message": result.get("final_answer") or "请补充售后申请信息",
                    "data": None,
                }

    return {
        "success": True,
        "code": "OK",
        "message": result.get("final_answer") or "处理成功",
        "data": None,
    }
