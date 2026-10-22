from types import SimpleNamespace
import logging

from core.logging import log_extra


from agent.input_analyzer import (
    analyze_agent_input,
    analyze_state_input
)
from agent.intent_router import route_intent


from agent.read_workflow import (
    run_read_workflow,
    READ_INTENT_TOOL_MAP
)


from agent.write_workflow import (
    run_write_workflow
)


from agent.state import (
    AgentState,
    create_agent_state
)


from agent.planner import (
    plan_state
)


from agent.context_resolver import (
    resolve_state_context
)


from services.task_service import (
    create_agent_task,
    get_active_agent_task,
    update_agent_task_context,
    get_task_context,
    update_task_context,
    merge_task_params,
    complete_agent_task,
    claim_agent_task_execution,
    new_agent_execution_id,
)
from agent.step_trace import trace_step
from agent.policy_response import read_result_failed
from agent.input_budget import InputAnalysisError
from agent.conversation_scope import current_scope, task_scope
from services.task_service import task_execution_lifecycle

logger = logging.getLogger("agent.runtime")


WRITE_INTENTS = {

    "create_after_sale"

}



@task_execution_lifecycle
def run_agent(
    user_text: str,
    current_user_id: int,
    trace_id: str,
    max_steps: int = 5
):


    """
    Agent统一运行入口
    """


    # =====================================================
    # 1. 获取当前用户是否存在未完成任务
    # =====================================================


    execution_id = new_agent_execution_id()
    active_task = get_active_agent_task(
        current_user_id
    )


    if active_task:


        task_id = active_task["id"]
        scope = current_scope()
        if not claim_agent_task_execution(
            task_id,
            current_user_id,
            scope.conversation_id if scope is not None else None,
            execution_id,
        ):
            return {
                "success": False,
                "code": "TASK_EXECUTION_CONFLICT",
                "message": "当前会话任务正在处理中，请稍后重试。",
                "data": {"task_id": task_id},
            }


    else:


        task = create_agent_task(

            user_id=current_user_id,

            task_type="agent",

            goal=user_text,

            intent="unknown",
            execution_id=execution_id,

        )


        task_id = task["task_id"]

    logger.info("task selected user_id=%s", current_user_id,
                extra=log_extra(trace_id=trace_id, task_id=task_id))

    old_context = get_task_context(task_id, execution_id)
    continuation_intent = None
    continuation_goal = None
    if active_task and active_task["status"] == "waiting_input":
        continuation_intent = old_context.get("_intent")
        if continuation_intent is None and old_context:
            # Existing waiting_input tasks were created before intent was saved.
            continuation_goal = active_task["goal"]
        if continuation_intent not in WRITE_INTENTS:
            continuation_intent = None


    # =====================================================
    # 2. 输入分析
    # =====================================================


    try:
        with trace_step(task_id, trace_id, "input_analysis", {
            "message_length": len(user_text)
        }) as step:
            input_analysis = analyze_agent_input(
                user_text, continuation_intent=continuation_intent,
                continuation_goal=continuation_goal, trace_id=trace_id, task_id=task_id,
            )
            step.set_result({
                "intent": input_analysis.intent,
                "params": vars(input_analysis.params)
            })
    except InputAnalysisError as exc:
        update_agent_task_context(
            task_id=task_id, current_step="failed", status="failed",
            execution_id=execution_id,
        )
        return exc.as_result()


    intent = input_analysis.intent



    # =====================================================
    # 3. 获取历史上下文
    # =====================================================


    logger.debug("old context fields=%s", sorted(old_context),
                 extra=log_extra(trace_id=trace_id, task_id=task_id))




    # 当前输入参数

    new_params = vars(
        input_analysis.params
    )
    correction = any(term in user_text for term in ("不是质量问题", "没有质量问题", "不确定是不是质量问题", "改成", "更正原因"))
    if intent in WRITE_INTENTS and (new_params.get("reason") or (old_context.get("reason") and correction)):
        if not new_params.get("reason") and correction:
            new_params["reason"] = user_text.strip()
        new_params["original_reason_text"] = user_text.strip()


    logger.debug("new parameter fields=%s", sorted(new_params),
                 extra=log_extra(trace_id=trace_id, task_id=task_id))




    # =====================================================
    # 4. 合并上下文
    # =====================================================


    with trace_step(task_id, trace_id, "context_merge", {
        "old_context": old_context, "new_params": new_params
    }) as step:
        merged_params = merge_task_params(old_context, new_params)
        if intent in WRITE_INTENTS:
            merged_params["_intent"] = intent
        step.set_result({"merged_params": merged_params})


    logger.debug("merged context fields=%s", sorted(merged_params),
                 extra=log_extra(trace_id=trace_id, task_id=task_id))




    # =====================================================
    # 5. 将合并后的参数重新注入
    # =====================================================


    input_analysis.params = SimpleNamespace(
        **merged_params
    )



    # =====================================================
    # 6. 保存上下文
    # =====================================================


    update_task_context(

        task_id=task_id,

        context=merged_params,
        execution_id=execution_id,

    )



    # =====================================================
    # 7. 更新任务状态
    # =====================================================


    update_agent_task_context(

        task_id=task_id,

        current_step=intent,

        status="running",
        execution_id=execution_id,

    )






    logger.info("input analyzed intent=%s param_fields=%s", intent,
                sorted(vars(input_analysis.params)),
                extra=log_extra(trace_id=trace_id, task_id=task_id))






    # =====================================================
    # 8. unknown处理
    # =====================================================


    if intent == "unknown":


        # 如果已有上下文，继续尝试

        if old_context:


            intent = old_context.get(
                "_intent",
                intent
            )


        else:


            update_agent_task_context(

                task_id=task_id,

                current_step="unknown",

                status="failed",
                execution_id=execution_id,

            )


            return (
                "我还不能确定你想处理什么，"
                "请再描述具体一些。"
            )



    # =====================================================
    # 9. 查询流程
    # =====================================================


    if intent in READ_INTENT_TOOL_MAP:
        try:
            with trace_step(task_id, trace_id, "read_workflow", {"intent": intent}) as step:
                result = run_read_workflow(
                    user_text=user_text, current_user_id=current_user_id,
                    input_analysis=input_analysis, max_steps=max_steps, trace_id=trace_id,
                )
                step.set_result({
                    "success": not read_result_failed(result),
                    "code": result.get("code") if isinstance(result, dict) else None,
                })
        except Exception:
            update_agent_task_context(
                task_id=task_id, current_step="failed", status="failed",
                execution_id=execution_id,
            )
            raise
        if read_result_failed(result):
            update_agent_task_context(
                task_id=task_id, current_step="failed", status="failed",
                execution_id=execution_id,
            )
            return result
        complete_agent_task(
            task_id,
            execution_id,
        )


        return result




    # =====================================================
    # 10. 写流程
    # =====================================================


    if intent in WRITE_INTENTS:

        merged_analysis = SimpleNamespace(
            intent=intent,
            params=SimpleNamespace(
                **merged_params
            )
        )

        logger.debug("write intent parameters merged", extra=log_extra(
            trace_id=trace_id, task_id=task_id
        ))

        with task_scope(task_id, execution_id):
            result = run_write_workflow(
                user_text=user_text,
                current_user_id=current_user_id,
                input_analysis=merged_analysis,
                trace_id=trace_id,
                task_id=task_id
            )



        if (

            isinstance(result, dict)

            and

            result.get("code")

            ==
            "ACTION_PREVIEW_READY"

        ):


            update_agent_task_context(

                task_id=task_id,

                current_step="waiting_confirm",

                status="waiting_confirm",
                execution_id=execution_id,

            )




        elif (

                isinstance(result, dict)

                and

                result.get("success") is False

        ):

            code = result.get("code")

            if code in {

                "AFTER_SALE_TYPE_REQUIRED",

                "AFTER_SALE_REASON_REQUIRED",

                "PRODUCT_NAME_REQUIRED"

            }:

                update_agent_task_context(

                    task_id=task_id,

                    current_step="waiting_input",

                    status="waiting_input",
                    execution_id=execution_id,

                )

            elif code in {"INELIGIBLE", "NEEDS_REVIEW"}:

                # A completed policy assessment is not a business execution
                # failure; no Pending Action has been created.
                update_agent_task_context(
                    task_id=task_id,
                    current_step="policy_check",
                    status="completed",
                    execution_id=execution_id,
                )


            else:

                update_agent_task_context(

                    task_id=task_id,

                    current_step="failed",

                    status="failed",
                    execution_id=execution_id,

                )


        else:


            complete_agent_task(
                task_id,
                execution_id,
            )



        return result




    # =====================================================
    # 11. 兜底
    # =====================================================


    update_agent_task_context(

        task_id=task_id,

        current_step="unsupported",

        status="failed",
        execution_id=execution_id,

    )


    return (
        "当前业务意图暂时没有对应的处理流程。"
    )






def prepare_agent_state(
    user_text: str,
    current_user_id: int,
    trace_id: str
) -> AgentState:


    """
    创建统一 AgentState
    """


    state = create_agent_state(

        user_text=user_text,

        current_user_id=current_user_id,

        trace_id=trace_id

    )



    analysis_update = analyze_state_input(
        state
    )


    state.update(
        analysis_update
    )



    if state["intent"] == "unknown":


        state["error"] = {

            "code":
                "UNKNOWN_INTENT",

            "message":
                "暂时无法识别用户意图"

        }

        return state

    planner_update = plan_state(
        state
    )

    state.update(
        planner_update
    )

    plan = state["plan"]

    if (

        plan is None

        or

        len(plan.steps) == 0

    ):

        return state

    first_step = plan.steps[0]

    if first_step.step_type == "resolve_context":

        context_update = resolve_state_context(
            state
        )

        state.update(
            context_update
        )

        planner_update = plan_state(
            state
        )

        state.update(
            planner_update
        )

    return state
