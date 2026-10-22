from langchain_core.tools import tool

from services.policy_rag_client import (
    query_policy_rag
)


@tool
def policy_rag_tool(
    question: str
) -> dict:
    """
    查询售后政策、退换货规则、FAQ 和标准处理流程。

    该工具只返回政策证据和解释，
    不负责判断最终业务资格，
    也不能直接创建、修改售后业务数据。
    """

    result = query_policy_rag(
        question=question
    )

    return result.model_dump()
