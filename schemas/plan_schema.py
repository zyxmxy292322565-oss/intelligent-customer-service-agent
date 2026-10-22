from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator
)


# =========================================================
# Planner 当前允许规划的 Intent
#
# unknown 不进入 Planner，
# 而应该直接进入澄清 / fallback。
# =========================================================

PlannableIntent = Literal[
    "query_order_detail",
    "query_after_sale_list",
    "query_after_sale_detail",
    "query_after_sale_history",
    "query_policy",
    "create_after_sale",
    "unknown"
]


# =========================================================
# Planner 可以生成的 Step 类型
# =========================================================

PlanStepType = Literal[
    "resolve_context",
    "call_tool",
    "clarify",
    "build_preview",
    "human_handoff",
    "finalize"
]


# =========================================================
# 当前真实存在并允许 Planner 使用的 Tool
# =========================================================

AllowedToolName = Literal[
    "get_order_detail_tool",
    "get_user_after_sales_tool",
    "get_after_sale_detail_tool",
    "get_after_sale_history_tool",
    "policy_rag_tool"
]


class PlanStep(BaseModel):

    model_config = ConfigDict(
        extra="forbid"
    )

    # 第几步
    step_id: int = Field(
        ge=1
    )

    # 这一步是什么类型
    step_type: PlanStepType

    # 给人看的步骤说明
    description: str = Field(
        min_length=1
    )

    # 只有 call_tool 才允许设置
    tool_name: AllowedToolName | None = None

    # 执行这一步之前需要哪些字段
    required_fields: list[str] = Field(
        default_factory=list
    )

    @model_validator(
        mode="after"
    )
    def validate_tool_usage(self):

        # call_tool 必须指定 Tool
        if (
            self.step_type == "call_tool"
            and
            self.tool_name is None
        ):
            raise ValueError(
                "call_tool 步骤必须指定 tool_name"
            )

        # 非 call_tool 不允许偷偷携带 Tool
        if (
            self.step_type != "call_tool"
            and
            self.tool_name is not None
        ):
            raise ValueError(
                "只有 call_tool 步骤允许指定 tool_name"
            )

        return self


class AgentPlan(BaseModel):

    model_config = ConfigDict(
        extra="forbid"
    )

    # 当前计划对应哪个 Intent
    intent: PlannableIntent

    # 最小可执行步骤
    steps: list[PlanStep] = Field(
        min_length=1
    )