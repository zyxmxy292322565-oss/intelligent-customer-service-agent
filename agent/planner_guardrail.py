from agent.state import AgentState

from schemas.plan_schema import (
    AgentPlan
)


# =========================================================
# 每个 Intent 允许调用的 Tool
# =========================================================

INTENT_ALLOWED_TOOLS = {

    "query_order_detail": {
        "get_order_detail_tool"
    },

    "query_after_sale_list": {
        "get_user_after_sales_tool"
    },

    "query_after_sale_detail": {
        "get_after_sale_detail_tool"
    },

    "query_after_sale_history": {
        "get_after_sale_history_tool"
    },

    "query_policy": {
        "policy_rag_tool"
    },

    # 创建售后当前不能让 Planner 直接调用业务写 Tool
    # 必须走 Preview → Confirm → Executor → Service
    "create_after_sale": {
        "policy_rag_tool"
    }
}


# =========================================================
# 每个 Intent 允许出现的 Step 类型
# =========================================================

INTENT_ALLOWED_STEP_TYPES = {

    "query_order_detail": {
        "resolve_context",
        "call_tool",
        "clarify",
        "finalize"
    },

    "query_after_sale_list": {
        "call_tool",
        "finalize"
    },

    "query_after_sale_detail": {
        "call_tool",
        "clarify",
        "finalize"
    },

    "query_after_sale_history": {
        "call_tool",
        "clarify",
        "finalize"
    },

    "query_policy": {
        "call_tool",
        "finalize",
    },

    "create_after_sale": {
        "resolve_context",
        "clarify",
        "build_preview",
        "human_handoff",
        "finalize",
        "call_tool"
    }
}


def validate_plan_for_state(
    state: AgentState,
    plan: AgentPlan
) -> AgentPlan:

    state_intent = state["intent"]

    # =====================================================
    # 1. State 必须已经有 Intent
    # =====================================================

    if state_intent is None:

        raise ValueError(
            "AgentState 中还没有 intent，"
            "不能校验 Planner 计划"
        )

    # =====================================================
    # 2. Plan 的 Intent 必须与 State 一致
    # =====================================================

    if plan.intent != state_intent:

        raise ValueError(
            f"Plan intent 与 AgentState 不一致："
            f"state={state_intent}, "
            f"plan={plan.intent}"
        )

    # =====================================================
    # 3. 检查 Step 编号必须连续
    # =====================================================

    expected_step_ids = list(
        range(
            1,
            len(plan.steps) + 1
        )
    )

    actual_step_ids = [
        step.step_id
        for step in plan.steps
    ]

    if (
        actual_step_ids
        != expected_step_ids
    ):

        raise ValueError(
            "Plan step_id 必须从 1 开始连续递增"
        )

    # =====================================================
    # 4. 当前 Intent 允许哪些 Step
    # =====================================================

    allowed_step_types = (
        INTENT_ALLOWED_STEP_TYPES[
            state_intent
        ]
    )

    # =====================================================
    # 5. 当前 Intent 允许哪些 Tool
    # =====================================================

    allowed_tools = (
        INTENT_ALLOWED_TOOLS[
            state_intent
        ]
    )

    # =====================================================
    # 6. 逐 Step 做确定性校验
    # =====================================================

    for step in plan.steps:

        if (
            step.step_type
            not in allowed_step_types
        ):

            raise ValueError(
                f"Intent {state_intent} "
                f"不允许 step_type="
                f"{step.step_type}"
            )

        if (
            step.tool_name is not None
            and
            step.tool_name
            not in allowed_tools
        ):

            raise ValueError(
                f"Intent {state_intent} "
                f"无权调用 Tool "
                f"{step.tool_name}"
            )

    return plan