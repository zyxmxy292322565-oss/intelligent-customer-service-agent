from langgraph.graph import (
    StateGraph,
    START,
    END
)

from agent.state import AgentState

from agent.graph.nodes import (
    analyze_node,
    planner_node,
    resolve_context_node,
    clarify_node,
    read_tool_node,
    build_preview_node,
    wait_confirmation_node,
    execute_action_node
)

from agent.graph.edges import (
    route_after_planner,
    route_after_analyze,
    route_after_confirmation,
    route_after_read_tool
)

import sqlite3

from pathlib import Path

from langgraph.checkpoint.sqlite import (
    SqliteSaver
)

from langgraph.checkpoint.serde.jsonplus import (
    JsonPlusSerializer
)

from schemas.plan_schema import (
    AgentPlan,
    PlanStep
)

# =====================================================
# Durable Checkpoint
# =====================================================

DATA_DIR = (
    Path(__file__)
    .resolve()
    .parents[2]
    / "data"
)

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

CHECKPOINT_DB_PATH = (
    DATA_DIR
    / "langgraph_checkpoints.sqlite3"
)


CHECKPOINT_CONNECTION = (
    sqlite3.connect(
        str(CHECKPOINT_DB_PATH),
        check_same_thread=False
    )
)


# =====================================================
# 只允许反序列化我们明确认可的自定义类型
# 同时解决 AgentPlan 的 warning
# =====================================================

CHECKPOINT_SERIALIZER = (
    JsonPlusSerializer(
        allowed_msgpack_modules=[
            AgentPlan,
            PlanStep
        ]
    )
)


CHECKPOINTER = SqliteSaver(
    CHECKPOINT_CONNECTION,
    serde=CHECKPOINT_SERIALIZER
)

def build_graph():

    # =====================================================
    # 1. 创建 Graph
    # =====================================================

    graph = StateGraph(
        AgentState
    )

    # =====================================================
    # 2. 注册 Nodes
    # =====================================================

    graph.add_node(
        "analyze",
        analyze_node
    )

    graph.add_node(
        "planner",
        planner_node
    )

    graph.add_node(
        "resolve_context",
        resolve_context_node
    )

    graph.add_node(
        "clarify",
        clarify_node
    )

    graph.add_node(
        "read_tool",
        read_tool_node
    )

    graph.add_node(
        "build_preview",
        build_preview_node
    )

    graph.add_node(
        "wait_confirmation",
        wait_confirmation_node
    )

    graph.add_node(
        "execute_action",
        execute_action_node
    )

    # =====================================================
    # 3. START
    # =====================================================

    # =====================================================
    # Graph Entry Point
    # =====================================================

    graph.add_edge(
        START,
        "analyze"
    )

    graph.add_conditional_edges(
        "read_tool",
        route_after_read_tool,
        {
            "planner":
                "planner",

            "end":
                END
        }
    )

    graph.add_conditional_edges(
        "analyze",
        route_after_analyze,
        {
            "planner":
                "planner",

            "clarify":
                "clarify"
        }
    )
    # =====================================================
    # 4. Planner Conditional Edge
    # =====================================================

    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "resolve_context":
                "resolve_context",

            "clarify":
                "clarify",

            "read_tool":
                "read_tool",

            "build_preview":
                "build_preview",

            "end":
                END
        }
    )

    # =====================================================
    # 5. Context Resolution 后重新 Planner
    # =====================================================

    graph.add_edge(
        "resolve_context",
        "planner"
    )

    # =====================================================
    # 6. 暂时的终点
    # =====================================================

    graph.add_edge(
        "clarify",
        END
    )

    graph.add_edge(
        "read_tool",
        END
    )

    graph.add_edge(
        "build_preview",
        "wait_confirmation"
    )

    graph.add_conditional_edges(
        "wait_confirmation",
        route_after_confirmation,
        {
            "execute_action":
                "execute_action",

            "end":
                END
        }
    )

    graph.add_edge(
        "execute_action",
        END
    )

    return graph.compile(
        checkpointer=CHECKPOINTER
    )