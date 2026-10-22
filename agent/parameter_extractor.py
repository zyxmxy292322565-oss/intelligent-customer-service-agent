from langchain_core.messages import (
    SystemMessage,
    HumanMessage
)

from llm.client import get_llm
from schemas.agent_schema import BusinessParams


def extract_business_params(
    user_text: str
) -> BusinessParams:

    llm = get_llm(disable_thinking=True, input_analysis=True)

    structured_llm = llm.with_structured_output(
        BusinessParams,
        method="function_calling"
    )

    messages = [
        SystemMessage(
            content=(
                "请从用户输入中提取业务参数。"
                "只提取用户明确表达的信息。"

                "如果用户表达仅退款，"
                "after_sale_type 返回 refund。"

                "如果用户表达退货退款，"
                "after_sale_type 返回 return_refund。"

                "如果用户表达换货，"
                "after_sale_type 返回 exchange。"

                "如果用户表达维修，"
                "after_sale_type 返回 repair。"

                "reason 提取用户描述的商品问题或售后原因。"

                "如果用户没有提供某个参数，"
                "该参数必须返回 null。"

                "禁止猜测或编造订单编号、售后编号。"
            )
        ),
        HumanMessage(
            content=user_text
        )
    ]

    result = structured_llm.invoke(
        messages
    )

    return result
