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

    只返回政策证据，
    不负责执行售后写操作。
    """

    result = query_policy_rag(
        question=question
    )

    return result.model_dump()