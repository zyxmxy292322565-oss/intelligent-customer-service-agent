from agent.state import AgentState

from schemas.plan_schema import (
    AgentPlan,
    PlanStep
)

from agent.planner_guardrail import (
    validate_plan_for_state
)

def build_deterministic_plan(
    state: AgentState
) -> AgentPlan:

    intent = state["intent"]
    params = state["params"]
    resolved_context = state["resolved_context"]
    error = state["error"]

    # =====================================================
    # Context Resolution 没找到对应业务数据
    # =====================================================

    if (
            error is not None
            and
            error.get("code")
            == "PRODUCT_NOT_FOUND"
    ):
        return AgentPlan(
            intent=intent,
            steps=[
                PlanStep(
                    step_id=1,
                    step_type="clarify",
                    description=(
                        "没有找到对应商品订单，"
                        "需要用户补充或修正商品/订单信息"
                    ),
                    required_fields=[
                        "product_name",
                        "order_no"
                    ]
                )
            ]
        )

    # =====================================================
    # Context Resolution 找到多个候选
    # =====================================================

    if (
            error is not None
            and
            error.get("code")
            == "MULTIPLE_PRODUCTS_FOUND"
    ):
        return AgentPlan(
            intent=intent,
            steps=[
                PlanStep(
                    step_id=1,
                    step_type="clarify",
                    description=(
                        "找到多个符合条件的商品订单，"
                        "需要用户从候选项中确认一个"
                    ),
                    required_fields=[
                        "order_no"
                    ]
                )
            ]
        )

    # =====================================================
    # 1. 查询订单详情
    # =====================================================

    if intent == "query_order_detail":

        order_no = (
            params.get("order_no")
            or
            resolved_context.get("order_no")
        )

        # 已有真实订单号，直接查询
        if order_no:

            return AgentPlan(
                intent=intent,
                steps=[
                    PlanStep(
                        step_id=1,
                        step_type="call_tool",
                        description="查询指定订单详情",
                        tool_name="get_order_detail_tool",
                        required_fields=[
                            "order_no"
                        ]
                    )
                ]
            )

        # 没订单号，但有商品名，可以尝试数据库解析
        if params.get("product_name"):

            return AgentPlan(
                intent=intent,
                steps=[
                    PlanStep(
                        step_id=1,
                        step_type="resolve_context",
                        description="根据商品名称解析真实订单",
                        required_fields=[
                            "product_name"
                        ]
                    ),
                    PlanStep(
                        step_id=2,
                        step_type="call_tool",
                        description="查询解析后的订单详情",
                        tool_name="get_order_detail_tool",
                        required_fields=[
                            "order_no"
                        ]
                    )
                ]
            )

        # 连可解析的信息都没有
        return AgentPlan(
            intent=intent,
            steps=[
                PlanStep(
                    step_id=1,
                    step_type="clarify",
                    description="需要用户补充订单号或商品名称",
                    required_fields=[
                        "order_no",
                        "product_name"
                    ]
                )
            ]
        )

    # =====================================================
    # 2. 查询当前用户全部售后
    # =====================================================

    if intent == "query_after_sale_list":

        return AgentPlan(
            intent=intent,
            steps=[
                PlanStep(
                    step_id=1,
                    step_type="call_tool",
                    description="查询当前用户的售后列表",
                    tool_name="get_user_after_sales_tool"
                )
            ]
        )

    # =====================================================
    # 3. 查询售后详情
    # =====================================================

    if intent == "query_after_sale_detail":

        after_sale_no = params.get(
            "after_sale_no"
        )

        if after_sale_no:

            return AgentPlan(
                intent=intent,
                steps=[
                    PlanStep(
                        step_id=1,
                        step_type="call_tool",
                        description="查询指定售后详情",
                        tool_name="get_after_sale_detail_tool",
                        required_fields=[
                            "after_sale_no"
                        ]
                    )
                ]
            )

        return AgentPlan(
            intent=intent,
            steps=[
                PlanStep(
                    step_id=1,
                    step_type="clarify",
                    description="需要用户补充售后编号",
                    required_fields=[
                        "after_sale_no"
                    ]
                )
            ]
        )

    # =====================================================
    # 4. 查询售后历史
    # =====================================================

    if intent == "query_after_sale_history":

        after_sale_no = params.get(
            "after_sale_no"
        )

        if after_sale_no:

            return AgentPlan(
                intent=intent,
                steps=[
                    PlanStep(
                        step_id=1,
                        step_type="call_tool",
                        description="查询指定售后的状态变化历史",
                        tool_name="get_after_sale_history_tool",
                        required_fields=[
                            "after_sale_no"
                        ]
                    )
                ]
            )

        return AgentPlan(
            intent=intent,
            steps=[
                PlanStep(
                    step_id=1,
                    step_type="clarify",
                    description="需要用户补充售后编号",
                    required_fields=[
                        "after_sale_no"
                    ]
                )
            ]
        )
    # ========================================
    #查询是否符合售后政策
    if intent == "query_policy":
        return AgentPlan(
            intent=intent,
            steps=[
                PlanStep(
                    step_id=1,
                    step_type="call_tool",
                    description="查询售后政策知识库",
                    tool_name="policy_rag_tool",
                    required_fields=[]
                )
            ]
        )

    # =====================================================
    # 5. 创建售后
    # =====================================================

    # =====================================================
    # 5. 创建售后
    # =====================================================

    if intent == "create_after_sale":

        missing_fields = []

        if not params.get(
                "product_name"
        ):
            missing_fields.append(
                "product_name"
            )

        if not params.get(
                "after_sale_type"
        ):
            missing_fields.append(
                "after_sale_type"
            )

        if not params.get(
                "reason"
        ):
            missing_fields.append(
                "reason"
            )

        # =====================================================
        # 1. 用户自然语言本身缺少必要参数
        # =====================================================

        if missing_fields:
            return AgentPlan(
                intent=intent,
                steps=[
                    PlanStep(
                        step_id=1,
                        step_type="clarify",
                        description=(
                            "创建售后所需信息不完整"
                        ),
                        required_fields=(
                            missing_fields
                        )
                    )
                ]
            )

        # =====================================================
        # 2. 检查真实业务上下文
        # =====================================================

        order_no = (
            resolved_context.get(
                "order_no"
            )
        )

        order_item_id = (
            resolved_context.get(
                "order_item_id"
            )
        )

        # =====================================================
        # 业务上下文还没解析
        # 先进入 Context Resolution
        #
        # 注意：
        # 这里只规划“当前下一步”，
        # Context Resolution 完成后会重新进入 Planner。
        # =====================================================

        if (
                not order_no
                or
                not order_item_id
        ):
            return AgentPlan(
                intent=intent,
                steps=[
                    PlanStep(
                        step_id=1,
                        step_type=(
                            "resolve_context"
                        ),
                        description=(
                            "根据商品名称解析真实订单"
                            "和订单商品"
                        ),
                        required_fields=[
                            "product_name"
                        ]
                    )
                ]
            )

        # =====================================================
        # 3. 判断本任务是否已经查询过 Policy RAG
        # =====================================================

        tool_results = (
                state["tool_results"]
                or []
        )

        policy_rag_executed = any(
            tool_result.get(
                "tool_name"
            )
            == "policy_rag_tool"

            for tool_result
            in tool_results
        )

        # =====================================================
        # 4. 真实订单已经解析，
        #    但还没有查询政策
        #
        # 先查询 Policy RAG。
        # =====================================================

        if not policy_rag_executed:
            return AgentPlan(
                intent=intent,
                steps=[
                    PlanStep(
                        step_id=1,
                        step_type="call_tool",
                        description=(
                            "查询与本次售后申请"
                            "相关的政策依据"
                        ),
                        tool_name=(
                            "policy_rag_tool"
                        ),
                        required_fields=[]
                    )
                ]
            )

        # =====================================================
        # 5. Context 已解析
        #    Policy RAG 也已经执行过
        #
        # 此时才能进入 Action Preview。
        # =====================================================

        return AgentPlan(
            intent=intent,
            steps=[
                PlanStep(
                    step_id=1,
                    step_type=(
                        "build_preview"
                    ),
                    description=(
                        "使用真实业务数据和"
                        "已获取的政策上下文"
                        "生成售后操作预览"
                    ),
                    required_fields=[
                        "order_no",
                        "order_item_id",
                        "product_name",
                        "after_sale_type",
                        "reason"
                    ]
                )
            ]
        )

    # =====================================================
    # 不应该进入 Planner 的 Intent
    # =====================================================

    raise ValueError(
        f"当前 Intent 不支持规划: {intent}"
    )

def plan_state(
    state: AgentState
) -> dict:

    # 1. 根据当前 State 生成计划
    plan = build_deterministic_plan(
        state
    )

    # 2. 经过确定性 Guardrail 校验
    validated_plan = (
        validate_plan_for_state(
            state,
            plan
        )
    )

    # 3. 只返回这个节点负责更新的字段
    return {
        "plan": validated_plan
    }