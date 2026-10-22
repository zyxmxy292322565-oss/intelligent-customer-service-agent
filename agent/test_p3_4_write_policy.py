from uuid import uuid4

from agent.state import (
    create_agent_state
)

from agent.graph.state_graph import (
    build_graph
)


def main():

    graph = build_graph()

    thread_id = (
        f"P3_4_"
        f"{uuid4().hex}"
    )

    config = {
        "configurable": {
            "thread_id":
                thread_id
        }
    }

    state = create_agent_state(
        user_text=(
            "无线键盘坏了，"
            "我想退货退款"
        ),
        current_user_id=1
    )

    result = graph.invoke(
        state,
        config=config
    )

    print(
        "\n===== P3-4 "
        "Write + Policy RAG ====="
    )

    print(
        "intent:",
        result.get("intent")
    )

    print(
        "\nresolved_context:"
    )
    print(
        result.get(
            "resolved_context"
        )
    )

    print(
        "\ntool_results:"
    )
    print(
        result.get(
            "tool_results"
        )
    )

    print(
        "\nconfirmation_id:"
    )
    print(
        result.get(
            "confirmation_id"
        )
    )

    print(
        "\ninterrupt:"
    )
    print(
        result.get(
            "__interrupt__"
        )
    )

    # =====================================================
    # 1. Intent
    # =====================================================

    assert (
        result["intent"]
        == "create_after_sale"
    )

    # =====================================================
    # 2. Context Resolution
    # =====================================================

    assert (
        result[
            "resolved_context"
        ].get(
            "order_no"
        )
        is not None
    )

    assert (
        result[
            "resolved_context"
        ].get(
            "order_item_id"
        )
        is not None
    )

    # =====================================================
    # 3. Policy RAG 只执行一次
    # =====================================================

    policy_results = [
        item
        for item
        in result[
            "tool_results"
        ]
        if item.get(
            "tool_name"
        )
        == "policy_rag_tool"
    ]

    assert len(
        policy_results
    ) == 1

    # =====================================================
    # 4. Policy Tool 输入必须是 question
    #    不是 user_id
    # =====================================================

    policy_call = (
        policy_results[0]
    )

    assert (
        "question"
        in policy_call[
            "input"
        ]
    )

    assert (
        "user_id"
        not in policy_call[
            "input"
        ]
    )

    # =====================================================
    # 5. 已得到 Policy Tool Result
    # =====================================================

    policy_result = (
        policy_call[
            "result"
        ]
    )

    assert isinstance(
        policy_result,
        dict
    )

    assert (
        "success"
        in policy_result
    )

    # =====================================================
    # 6. 已成功进入 Action Preview
    # =====================================================

    assert (
        result.get(
            "confirmation_id"
        )
        is not None
    )

    print(
        "\n===== Test Summary ====="
    )

    print(
        "PASS：create_after_sale 已完成 "
        "Context Resolution "
        "→ Policy RAG "
        "→ Planner "
        "→ Action Preview "
        "→ Interrupt。"
    )


if __name__ == "__main__":

    main()