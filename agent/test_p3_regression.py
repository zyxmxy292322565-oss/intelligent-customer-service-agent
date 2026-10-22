from uuid import uuid4

from agent.state import (
    create_agent_state
)

from agent.graph.state_graph import (
    build_graph
)

from tools.policy_rag_tool import (
    policy_rag_tool
)


# =====================================================
# 工具函数
# =====================================================

def new_config(
    prefix: str
) -> dict:

    return {
        "configurable": {
            "thread_id": (
                f"{prefix}_"
                f"{uuid4().hex}"
            )
        }
    }


# =====================================================
# Case 1
# query_policy 正常命中
# =====================================================

def test_policy_query_hit(
    graph
):

    print(
        "\n"
        "========================================"
    )
    print(
        "Case 1: Policy Query Hit"
    )
    print(
        "========================================"
    )

    state = create_agent_state(
        user_text=(
            "耳机用了20天还能退吗？"
        ),
        current_user_id=1
    )

    result = graph.invoke(
        state,
        config=new_config(
            "P3_POLICY_HIT"
        )
    )

    print(
        "intent:",
        result.get(
            "intent"
        )
    )

    print(
        "tool_results:",
        result.get(
            "tool_results"
        )
    )

    print(
        "error:",
        result.get(
            "error"
        )
    )

    # -----------------------------
    # Intent
    # -----------------------------

    assert (
        result["intent"]
        == "query_policy"
    )

    # -----------------------------
    # Tool Result
    # -----------------------------

    tool_results = (
        result.get(
            "tool_results"
        )
        or []
    )

    policy_results = [
        item
        for item
        in tool_results
        if item.get(
            "tool_name"
        )
        == "policy_rag_tool"
    ]

    assert (
        len(policy_results)
        == 1
    )

    policy_result = (
        policy_results[0][
            "result"
        ]
    )

    assert (
        policy_result[
            "success"
        ]
        is True
    )

    assert (
        policy_result[
            "refused"
        ]
        is False
    )

    assert (
        len(
            policy_result[
                "evidence"
            ]
        )
        >= 1
    )

    print(
        "PASS：query_policy "
        "正常命中 Policy Evidence"
    )


# =====================================================
# Case 2
# RAG 无相关政策时必须拒答
#
# 这里直接测 policy_rag_tool，
# 避免 Router 把完全无关问题判成 unknown，
# 从而测不到 RAG 自己的拒答能力。
# =====================================================

def test_policy_refusal():

    print(
        "\n"
        "========================================"
    )
    print(
        "Case 2: Policy RAG Refusal"
    )
    print(
        "========================================"
    )

    result = policy_rag_tool.invoke({
        "question":
            "重庆明天天气怎么样？"
    })

    print(
        "result:",
        result
    )

    assert (
        result[
            "success"
        ]
        is True
    )

    assert (
        result[
            "refused"
        ]
        is True
    )

    assert (
        result[
            "code"
        ]
        == "POLICY_NOT_FOUND"
    )

    assert (
        result[
            "evidence"
        ]
        == []
    )

    print(
        "PASS：无相关政策时 "
        "Policy RAG 正确拒答"
    )


# =====================================================
# Case 3
# create_after_sale
# Context → Policy → Preview → Interrupt
# =====================================================

def test_create_after_sale_policy(
    graph
):

    print(
        "\n"
        "========================================"
    )
    print(
        "Case 3: "
        "Create After Sale + Policy RAG"
    )
    print(
        "========================================"
    )

    state = create_agent_state(
        user_text=(
            "无线键盘坏了，"
            "我想退货退款"
        ),
        current_user_id=1
    )

    result = graph.invoke(
        state,
        config=new_config(
            "P3_WRITE_POLICY"
        )
    )

    print(
        "intent:",
        result.get(
            "intent"
        )
    )

    print(
        "resolved_context:",
        result.get(
            "resolved_context"
        )
    )

    print(
        "tool_results:",
        result.get(
            "tool_results"
        )
    )

    print(
        "confirmation_id:",
        result.get(
            "confirmation_id"
        )
    )

    print(
        "interrupt:",
        result.get(
            "__interrupt__"
        )
    )

    # -----------------------------
    # Intent
    # -----------------------------

    assert (
        result[
            "intent"
        ]
        == "create_after_sale"
    )

    # -----------------------------
    # Context Resolution
    # -----------------------------

    context = (
        result[
            "resolved_context"
        ]
    )

    assert (
        context.get(
            "order_no"
        )
        is not None
    )

    assert (
        context.get(
            "order_item_id"
        )
        is not None
    )

    # -----------------------------
    # Policy RAG 只能执行一次
    # -----------------------------

    tool_results = (
        result.get(
            "tool_results"
        )
        or []
    )

    policy_results = [
        item
        for item
        in tool_results
        if item.get(
            "tool_name"
        )
        == "policy_rag_tool"
    ]

    assert (
        len(policy_results)
        == 1
    )

    policy_result = (
        policy_results[0][
            "result"
        ]
    )

    assert (
        policy_result[
            "success"
        ]
        is True
    )

    assert (
        policy_result[
            "refused"
        ]
        is False
    )

    assert (
        len(
            policy_result[
                "evidence"
            ]
        )
        >= 1
    )

    # -----------------------------
    # Pending Action
    # -----------------------------

    assert (
        result.get(
            "confirmation_id"
        )
        is not None
    )

    # -----------------------------
    # LangGraph 必须暂停等待确认
    # -----------------------------

    interrupts = (
        result.get(
            "__interrupt__"
        )
        or []
    )

    assert (
        len(interrupts)
        == 1
    )

    interrupt_value = (
        interrupts[0].value
    )

    assert (
        interrupt_value[
            "type"
        ]
        == "action_confirmation"
    )

    preview = (
        interrupt_value[
            "preview"
        ]
    )

    # -----------------------------
    # Preview 中必须带 Policy
    # -----------------------------

    assert (
        "policy"
        in preview
    )

    assert (
        preview[
            "policy"
        ][
            "found"
        ]
        is True
    )

    assert (
        preview[
            "policy"
        ][
            "source"
        ]
        is not None
    )

    print(
        "PASS：create_after_sale "
        "已完成 Context Resolution "
        "→ Policy RAG "
        "→ Preview "
        "→ Interrupt"
    )


# =====================================================
# Main
# =====================================================

def main():

    print(
        "\n"
        "========================================"
    )
    print(
        "P3 Regression Test"
    )
    print(
        "========================================"
    )

    graph = build_graph()

    test_policy_query_hit(
        graph
    )

    test_policy_refusal()

    test_create_after_sale_policy(
        graph
    )

    print(
        "\n"
        "========================================"
    )
    print(
        "P3 Regression Test Summary"
    )
    print(
        "========================================"
    )

    print(
        "PASS：P3 三条核心链路全部通过"
    )

    print(
        "1. query_policy 正常命中"
    )

    print(
        "2. 无相关政策正确拒答"
    )

    print(
        "3. create_after_sale "
        "接入 Policy RAG "
        "并进入 Action Preview"
    )


if __name__ == "__main__":
    main()