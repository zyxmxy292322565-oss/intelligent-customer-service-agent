from agent.state import AgentState


def route_after_planner(
    state: AgentState
) -> str:

    plan = state["plan"]

    # 没有计划
    if (
        plan is None
        or
        len(plan.steps) == 0
    ):
        return "end"

    first_step = plan.steps[0]

    step_type = first_step.step_type

    # =====================================================
    # Planner Step → Graph Node
    # =====================================================

    route_map = {
        "resolve_context":
            "resolve_context",

        "clarify":
            "clarify",

        "call_tool":
            "read_tool",

        "build_preview":
            "build_preview",

        "finalize":
            "end",

        "human_handoff":
            "end"
    }

    return route_map.get(
        step_type,
        "end"
    )

def route_after_confirmation(
    state: AgentState
) -> str:

    if state["error"] is not None:

        return "end"

    return "execute_action"

def route_after_analyze(
    state: AgentState
) -> str:

    if state["intent"] == "unknown":

        return "clarify"

    return "planner"

def route_after_read_tool(
    state: AgentState
) -> str:

    # =====================================================
    # Node 自身发生错误
    # =====================================================

    if state["error"] is not None:

        return "end"

    # =====================================================
    # create_after_sale 中的 Policy RAG
    #
    # Tool 执行完后不是结束，
    # 而是重新进入 Planner。
    # =====================================================

    if (
        state["intent"]
        == "create_after_sale"
    ):

        return "planner"

    # =====================================================
    # 普通查询 Tool 执行完成即可结束
    # =====================================================

    return "end"