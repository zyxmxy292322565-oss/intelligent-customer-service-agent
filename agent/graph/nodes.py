from agent.state import AgentState
from agent.input_analyzer import analyze_state_input
from agent.planner import plan_state
from agent.context_resolver import resolve_state_context

from tools.order_tools import (
    get_order_detail_tool
)

from tools.after_sale_tools import (
    get_user_after_sales_tool,
    get_after_sale_detail_tool,
    get_after_sale_history_tool
)

from langgraph.types import interrupt

from agent.write_workflow import (
    build_create_after_sale_pending_action
)

from agent.confirmation_service import (
    confirm_action
)

from agent.action_executor import (
    execute_confirmed_action
)

from tools.policy_rag_tool import (
    policy_rag_tool
)


# =====================================================
# Read Tool Registry
# =====================================================

READ_TOOL_REGISTRY = {
    "get_order_detail_tool":
        get_order_detail_tool,

    "get_user_after_sales_tool":
        get_user_after_sales_tool,

    "get_after_sale_detail_tool":
        get_after_sale_detail_tool,

    "get_after_sale_history_tool":
        get_after_sale_history_tool,

    "policy_rag_tool":
        policy_rag_tool,
}

def get_policy_preview(
    state: AgentState
) -> dict | None:

    tool_results = (
        state["tool_results"]
        or []
    )

    # 从后往前找，
    # 优先使用最近一次 Policy RAG 调用结果
    for tool_result in reversed(
        tool_results
    ):

        if (
            tool_result.get("tool_name")
            != "policy_rag_tool"
        ):
            continue

        result = (
            tool_result.get("result")
            or {}
        )

        refused = result.get(
            "refused",
            False
        )

        evidence = (
            result.get("evidence")
            or []
        )

        # =============================================
        # RAG 已执行，但没有可靠政策证据
        # =============================================

        if (
            refused
            or
            len(evidence) == 0
        ):

            return {
                "found": False,
                "refused": True,
                "message": (
                    result.get("message")
                    or
                    "未检索到足够相关的政策依据"
                )
            }

        # =============================================
        # 当前 Preview 只展示 Top1 Evidence
        # =============================================

        top_evidence = evidence[0]

        return {
            "found": True,
            "refused": False,

            "source":
                top_evidence.get(
                    "source"
                ),

            "chunk_id":
                top_evidence.get(
                    "chunk_id"
                ),

            "version":
                top_evidence.get(
                    "version"
                ),

            "rerank_score":
                top_evidence.get(
                    "rerank_score"
                ),

            "content":
                top_evidence.get(
                    "content"
                )
        }

    # 根本没有执行过 Policy Tool
    return None

# =====================================================
# 1. Input Analyzer Node
# =====================================================

def analyze_node(
    state: AgentState
) -> dict:

    return analyze_state_input(
        state
    )


# =====================================================
# 2. Planner Node
# =====================================================

def planner_node(
    state: AgentState
) -> dict:

    return plan_state(
        state
    )


# =====================================================
# 3. Context Resolution Node
# =====================================================

def resolve_context_node(
    state: AgentState
) -> dict:

    return resolve_state_context(
        state
    )


# =====================================================
# 4. Clarify Node
# =====================================================

def clarify_node(
    state: AgentState
) -> dict:

    plan = state["plan"]

    if (
        plan is None
        or
        len(plan.steps) == 0
    ):

        return {
            "final_answer":
                "当前信息不足，需要补充信息。"
        }

    step = plan.steps[0]

    context = state[
        "resolved_context"
    ]

    candidates = context.get(
        "candidates",
        []
    )

    # =====================================================
    # 有候选列表
    # =====================================================

    if candidates:

        candidate_lines = []

        for candidate in candidates:

            order_no = (
                candidate.get(
                    "order_no"
                )
            )

            product_name = (
                candidate.get(
                    "product_name"
                )
            )

            candidate_lines.append(
                f"- {order_no}："
                f"{product_name}"
            )

        candidate_text = "\n".join(
            candidate_lines
        )

        return {
            "final_answer": (
                f"{step.description}\n"
                f"{candidate_text}"
            )
        }

    # =====================================================
    # 普通缺参
    # =====================================================

    required_fields = (
        step.required_fields
    )

    return {
        "final_answer": (
            f"{step.description}。"
            f"需要补充："
            f"{', '.join(required_fields)}"
        )
    }


# =====================================================
# 5. Read Tool Node
# =====================================================

def read_tool_node(
    state: AgentState
) -> dict:

    plan = state["plan"]

    # =====================================================
    # 1. 检查 Planner 是否生成了可执行计划
    # =====================================================

    if (
        plan is None
        or
        len(plan.steps) == 0
    ):
        return {
            "error": {
                "code":
                    "READ_PLAN_NOT_FOUND",

                "message":
                    "没有可执行的读取计划"
            }
        }

    step = plan.steps[0]

    tool_name = step.tool_name

    # =====================================================
    # 2. Tool 白名单检查
    # =====================================================

    if (
        tool_name is None
        or
        tool_name not in READ_TOOL_REGISTRY
    ):
        return {
            "error": {
                "code":
                    "READ_TOOL_NOT_ALLOWED",

                "message": (
                    f"不允许调用 Tool: "
                    f"{tool_name}"
                )
            }
        }

    tool = READ_TOOL_REGISTRY[
        tool_name
    ]

    params = state["params"]

    context = state[
        "resolved_context"
    ]

    # =====================================================
    # 3. 根据不同 Tool 构造参数
    # =====================================================
    #
    # 注意：
    # 业务 Tool 查询用户私有业务数据，
    # 必须使用可信 current_user_id。
    #
    # Policy RAG 查询公共政策知识，
    # 不需要 user_id，只需要原始 question。
    # =====================================================

    if tool_name == "policy_rag_tool":

        tool_args = {
            "question":
                state["user_text"]
        }

    elif tool_name == "get_order_detail_tool":

        tool_args = {
            "user_id":
                state["current_user_id"],

            "order_no": (
                params.get(
                    "order_no"
                )
                or
                context.get(
                    "order_no"
                )
            )
        }

    elif tool_name == "get_user_after_sales_tool":

        tool_args = {
            "user_id":
                state["current_user_id"]
        }

    elif tool_name == "get_after_sale_detail_tool":

        tool_args = {
            "user_id":
                state["current_user_id"],

            "after_sale_no": (
                params.get(
                    "after_sale_no"
                )
                or
                context.get(
                    "after_sale_no"
                )
            )
        }

    elif tool_name == "get_after_sale_history_tool":

        tool_args = {
            "user_id":
                state["current_user_id"],

            "after_sale_no": (
                params.get(
                    "after_sale_no"
                )
                or
                context.get(
                    "after_sale_no"
                )
            )
        }

    else:

        # 理论上前面的 Registry 校验已经会拦住，
        # 这里作为最后一道兜底。
        return {
            "error": {
                "code":
                    "READ_TOOL_ARGS_NOT_SUPPORTED",

                "message": (
                    f"暂不支持为 Tool "
                    f"{tool_name} 构造参数"
                )
            }
        }

    # =====================================================
    # 4. 兼容 LangChain Tool / 普通 Python Function
    # =====================================================

    if hasattr(
        tool,
        "invoke"
    ):

        result = tool.invoke(
            tool_args
        )

    else:

        result = tool(
            **tool_args
        )

    # =====================================================
    # 5. 保存 Tool Result
    # =====================================================

    tool_result = {
        "tool_name":
            tool_name,

        "input":
            tool_args,

        "result":
            result
    }

    old_results = (
        state["tool_results"]
        or []
    )

    return {
        "tool_results":
            old_results
            + [tool_result],

        "final_answer":
            str(result),

        "error":
            None
    }


# =====================================================
# 6. Build Preview Node
# =====================================================

def build_preview_node(
    state: AgentState
) -> dict:

    params = dict(state["params"])
    if params.get("reason") and not params.get("original_reason_text"):
        params["original_reason_text"] = state.get("user_text", "").strip()
    result = (
        build_create_after_sale_pending_action(
            current_user_id=
                state["current_user_id"],

            params=params,

            resolved_context=
                state["resolved_context"],

            trace_id=state.get("trace_id")
        )
    )

    if not result["success"]:

        return {
            "error": {
                "code":
                    result["code"],

                "message":
                    result["message"],

                "data":
                    result.get("data")
            }
        }

    return {
        "confirmation_id":
            result["data"][
                "confirmation_id"
            ],

        "action_preview": result["data"]["preview"],

        "final_answer":
            result["message"],

        "error":
            None
    }


# =====================================================
# 7. Wait Confirmation Node
# =====================================================

def wait_confirmation_node(
    state: AgentState
) -> dict:

    # build_preview may have stopped on a policy decision. Preserve that
    # decision instead of replacing it with CONFIRMATION_ID_NOT_FOUND.
    if state.get("error") is not None:
        return {}

    confirmation_id = (
        state["confirmation_id"]
    )

    if confirmation_id is None:

        return {
            "error": {
                "code":
                    "CONFIRMATION_ID_NOT_FOUND",

                "message":
                    "当前任务没有待确认操作"
            }
        }

    preview = state.get("action_preview")
    if not preview:
        return {
            "error": {
                "code": "PREVIEW_NOT_FOUND",
                "message": "当前任务没有可确认的售后预览",
            }
        }

    # =====================================================
    # 在这里暂停 Graph
    # =====================================================

    approved = interrupt({
        "type":
            "action_confirmation",

        "confirmation_id":
            confirmation_id,

        "message":
            "请确认是否执行该售后操作",

        "preview": preview
    })

    # =====================================================
    # 用户不同意
    # =====================================================

    if approved is not True:

        return {
            "final_answer":
                "本次操作未确认，不会执行。",

            "error": {
                "code":
                    "ACTION_REJECTED_BY_USER",

                "message":
                    "用户未确认写操作"
            }
        }

    # =====================================================
    # 用户确认后调用原来的 Confirmation Service
    # =====================================================

    result = confirm_action(
        confirmation_id=
            confirmation_id,

        current_user_id=
            state["current_user_id"]
    )

    if not result["success"] and result["code"] != "ACTION_ALREADY_CONFIRMED":

        return {
            "error": {
                "code":
                    result["code"],

                "message":
                    result["message"],

                "data":
                    result.get("data")
            }
        }

    return {
        "final_answer":
            result["message"],

        "error":
            None
    }


# =====================================================
# 8. Execute Action Node
# =====================================================

def execute_action_node(
    state: AgentState
) -> dict:

    confirmation_id = (
        state["confirmation_id"]
    )

    if confirmation_id is None:

        return {
            "error": {
                "code":
                    "CONFIRMATION_ID_NOT_FOUND",

                "message":
                    "没有可以执行的确认操作"
            }
        }

    result = (
        execute_confirmed_action(
            confirmation_id=
                confirmation_id,

            current_user_id=
                state["current_user_id"]
        )
    )

    if not result["success"]:

        return {
            "final_answer":
                result["message"],

            "error": {
                "code":
                    result["code"],

                "message":
                    result["message"],

                "data":
                    result.get("data")
            }
        }

    return {
        "final_answer":
            result["message"],

        "error":
            None
    }
