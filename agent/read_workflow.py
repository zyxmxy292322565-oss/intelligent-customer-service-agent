from langchain_core.messages import (
    SystemMessage,
    HumanMessage,
    ToolMessage
)
import logging
from core.logging import log_extra

logger = logging.getLogger("agent.read_workflow")

from llm.client import get_llm

from tools.order_tools import (
    get_order_detail_tool
)

from tools.after_sale_tools import (
    get_user_after_sales_tool,
    get_after_sale_detail_tool,
    get_after_sale_history_tool
)
from tools.policy_rag_tool import policy_rag_tool
from agent.policy_response import format_policy_result


# Read Intent Tool Map（只读意图工具映射）
READ_INTENT_TOOL_MAP = {
    "query_policy": [policy_rag_tool],
    "query_order_detail": [
        get_order_detail_tool
    ],

    "query_after_sale_list": [
        get_user_after_sales_tool
    ],

    "query_after_sale_detail": [
        get_user_after_sales_tool,
        get_after_sale_detail_tool
    ],

    "query_after_sale_history": [
        get_user_after_sales_tool,
        get_after_sale_history_tool
    ]
}


def run_read_workflow(
    user_text: str,
    current_user_id: int,
    input_analysis,
    max_steps: int = 5,
    trace_id: str | None = None
):
    """
    执行只读业务工作流。
    """

    intent = input_analysis.intent

    # 政策问题必须检索证据，不交给模型选择是否调用工具。
    if intent == "query_policy":
        result = policy_rag_tool.invoke({"question": user_text})
        return format_policy_result(result, user_text)

    # 1. 根据 Intent 获取允许使用的工具
    allowed_tools = READ_INTENT_TOOL_MAP.get(
        intent,
        []
    )

    if not allowed_tools:
        return (
            "当前只读工作流没有可执行的工具。"
        )

    logger.info("read workflow started intent=%s allowed_tools=%s", intent,
                [tool.name for tool in allowed_tools], extra=log_extra(trace_id=trace_id))

    # 2. 给 LLM 绑定当前 Workflow 允许使用的工具
    llm = get_llm()

    llm_with_tools = llm.bind_tools(
        allowed_tools
    )

    # 3. Tool Registry（工具注册表）
    tool_map = {
        tool.name: tool
        for tool in allowed_tools
    }

    # 4. 构建消息
    messages = [
        SystemMessage(
            content=(
                f"当前登录用户ID为 {current_user_id}。"

                "你现在处于只读业务工作流。"
                "只能查询数据，不能修改任何业务数据。"

                f"用户业务意图为："
                f"{input_analysis.intent}。"

                f"业务参数为："
                f"{input_analysis.params.model_dump()}。"

                "值为 None 的参数表示用户没有明确提供，"
                "禁止猜测或编造订单编号、售后编号。"

                "如果缺少必要参数，"
                "可以调用允许的只读工具查询真实业务数据。"

                "如果工具需要 user_id 参数，"
                "必须使用当前登录用户ID。"

                "只调用完成当前任务真正需要的工具。"
            )
        ),

        HumanMessage(
            content=user_text
        )
    ]

    # 5. Agent Loop（智能体循环）
    for step in range(max_steps):

        logger.debug("read agent step=%s", step + 1, extra=log_extra(trace_id=trace_id))

        ai_message = llm_with_tools.invoke(
            messages
        )

        messages.append(
            ai_message
        )

        # 没有新的 Tool Call，结束流程
        if not ai_message.tool_calls:

            logger.info("read workflow completed", extra=log_extra(trace_id=trace_id))

            return ai_message.content

        # 执行这一轮所有 Tool Call
        for tool_call in ai_message.tool_calls:

            tool_name = tool_call["name"]

            tool_args = dict(
                tool_call["args"]
            )

            tool_call_id = tool_call["id"]

            logger.debug("tool selected name=%s arg_fields=%s", tool_name,
                         sorted(tool_args), extra=log_extra(trace_id=trace_id))

            selected_tool = tool_map.get(
                tool_name
            )

            if selected_tool is None:

                tool_result = {
                    "success": False,
                    "code": "TOOL_NOT_FOUND",
                    "message": (
                        f"未知工具：{tool_name}"
                    )
                }

            else:

                tool_schema = (
                    selected_tool
                    .args_schema
                    .model_json_schema()
                )

                tool_properties = (
                    tool_schema.get(
                        "properties",
                        {}
                    )
                )

                # Trusted Context（可信上下文）
                if "user_id" in tool_properties:
                    tool_args["user_id"] = (
                        current_user_id
                    )

                logger.debug("tool invocation name=%s", tool_name,
                             extra=log_extra(trace_id=trace_id))

                # 真正执行 Tool
                tool_result = (
                    selected_tool.invoke(
                        tool_args
                    )
                )

            logger.debug("tool completed name=%s", tool_name,
                         extra=log_extra(trace_id=trace_id))

            # 把 Tool Result 返回 LLM
            messages.append(
                ToolMessage(
                    content=str(
                        tool_result
                    ),
                    tool_call_id=(
                        tool_call_id
                    )
                )
            )

    logger.warning("read workflow max steps reached", extra=log_extra(trace_id=trace_id))

    return (
        "查询任务执行超过最大步数，"
        "请稍后重试"
    )
