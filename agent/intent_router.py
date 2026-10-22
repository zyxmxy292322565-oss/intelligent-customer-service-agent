from langchain_core.messages import (
    SystemMessage,
    HumanMessage
)

from llm.client import get_llm
from schemas.agent_schema import IntentResult


def route_intent(
    user_text: str
) -> IntentResult:

    llm = get_llm(disable_thinking=True, input_analysis=True)

    structured_llm = llm.with_structured_output(
        IntentResult,
        method="function_calling"
    )

    messages = [
        SystemMessage(
            content=(
                "你是消费订单与售后系统的意图路由器。"
                "请判断用户当前最主要的业务意图。"

                "query_order_detail："
                "查询具体订单状态、金额、商品等。"

                "query_after_sale_list："
                "查询用户有哪些售后申请。"

                "query_after_sale_detail："
                "查询某个售后目前状态、退款金额、"
                "售后类型等当前信息。"

                "query_after_sale_history："
                "查询售后的处理过程、状态变化历史、"
                "什么时候进入某个状态。"

                "query_policy："
                "用户询问售后政策、退换货规则、保修规则、退款条件、"
                "商品是否符合某类售后政策等政策性问题；"
                "例如能不能退、用了20天还能退吗、超过多少天可以退。"
                "询问规则时选 query_policy；明确要求现在创建售后申请时才选 create_after_sale。"
                "即使商品陌生、听起来不现实或政策可能不存在，只要用户在询问退款承诺、"
                "售后条款或申请规则，仍选 query_policy，而不是 unknown；"
                "由只读政策检索判断是否有依据，意图识别不得替代政策存在性核验。"

                "create_after_sale："
                "用户想申请退款、退货退款、换货或维修。"

                "unknown："
                "无法判断或不属于以上业务意图。"

                "只根据用户当前表达判断意图，"
                "不要编造业务数据。"
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
