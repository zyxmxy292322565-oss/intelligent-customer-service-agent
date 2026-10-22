from agent.parameter_extractor import (
    extract_business_params
)

from agent.intent_router import (
    route_intent
)

from schemas.agent_schema import (
    AgentInputAnalysis
)

from agent.state import AgentState
from services.task_service import merge_task_params
from time import monotonic
from agent.input_budget import INPUT_ANALYSIS_BUDGET_SECONDS, InputAnalysisError, run_analysis_stage
from schemas.agent_schema import BusinessParams, IntentResult


_REASON_CORRECTION_TERMS = (
    "不是质量问题",
    "没有质量问题",
    "不确定是不是质量问题",
    "改成",
    "更正原因",
)

def analyze_agent_input(
    user_text: str,
    continuation_intent: str | None = None,
    *, trace_id: str | None = None, task_id: int | str | None = None,
    continuation_goal: str | None = None,
) -> AgentInputAnalysis:
    """
    对用户输入进行统一分析：
    1. 识别业务意图
    2. 提取业务参数
    """

    deadline = monotonic() + INPUT_ANALYSIS_BUDGET_SECONDS
    if continuation_goal and not continuation_intent:
        previous = run_analysis_stage("intent_recovery", route_intent, continuation_goal, deadline, trace_id=trace_id, task_id=task_id, expected_type=IntentResult)
        continuation_intent = previous.intent if previous.intent == "create_after_sale" else None
    if continuation_intent:
        intent = continuation_intent
    else:
        result = run_analysis_stage("intent", route_intent, user_text, deadline, trace_id=trace_id, task_id=task_id, expected_type=IntentResult)
        intent = result.intent
    business_params = run_analysis_stage("parameters", extract_business_params, user_text, deadline, trace_id=trace_id, task_id=task_id, expected_type=BusinessParams)
    # Keep the actual user utterance for deterministic policy classification.
    # The LLM may shorten "没有质量问题，但是左边没声音" to "左边没声音".
    if business_params.reason or any(term in user_text for term in (
        "不是质量问题", "没有质量问题", "不确定是不是质量问题", "更正原因"
    )):
        if not business_params.reason:
            business_params.reason = user_text.strip()
        business_params.original_reason_text = user_text.strip()

    result = AgentInputAnalysis(
        intent=intent,
        params=business_params
    )

    return result

def analyze_state_input(
    state: AgentState
) -> dict:

    input_analysis = analyze_agent_input(
        state["user_text"],
        continuation_intent=state.get("continuation_intent"),
        trace_id=state.get("trace_id"), task_id=state.get("task_id"),
        continuation_goal=state.get("continuation_goal"),
    )

    new_params = input_analysis.params.model_dump()
    if input_analysis.params.original_reason_text:
        new_params["original_reason_text"] = input_analysis.params.original_reason_text

    old_context = dict(state.get("task_context") or {})
    correction = any(term in state["user_text"] for term in _REASON_CORRECTION_TERMS)
    if (
        input_analysis.intent == "create_after_sale"
        and (new_params.get("reason") or (old_context.get("reason") and correction))
    ):
        # A current-turn reason is authoritative.  Replacing both fields keeps
        # corrections deterministic and avoids concatenating contradictory
        # historical utterances.
        if not new_params.get("reason") and correction:
            new_params["reason"] = state["user_text"].strip()
        new_params["original_reason_text"] = state["user_text"].strip()

    params = merge_task_params(old_context, new_params)
    if input_analysis.intent == "create_after_sale":
        params["_intent"] = input_analysis.intent
    return {"intent": input_analysis.intent, "params": params}
