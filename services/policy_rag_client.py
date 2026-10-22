import httpx
from core.settings import get_settings

from pydantic import ValidationError

from schemas.policy_rag_schema import (
    PolicyRAGRequest,
    PolicyRAGResponse
)


_settings = get_settings()
POLICY_RAG_SERVICE_URL = _settings.policy_rag_service_url
POLICY_RAG_TIMEOUT_SECONDS = _settings.policy_rag_timeout_seconds


def query_policy_rag(
    question: str,
    top_k: int = 5
) -> PolicyRAGResponse:

    request_data = (
        PolicyRAGRequest(
            question=question,
            top_k=top_k
        )
    )

    url = (
        f"{POLICY_RAG_SERVICE_URL}"
        f"/rag/query"
    )

    try:

        response = httpx.post(
            url,
            json=request_data.model_dump(),
            timeout=(
                POLICY_RAG_TIMEOUT_SECONDS
            )
        )

        response.raise_for_status()

    except httpx.TimeoutException:

        return PolicyRAGResponse(
            success=False,
            code="RAG_TIMEOUT",
            message="政策知识服务响应超时"
        )

    except httpx.HTTPError as exc:

        return PolicyRAGResponse(
            success=False,
            code="RAG_SERVICE_UNAVAILABLE",
            message=(
                "政策知识服务暂时不可用："
                f"{exc}"
            )
        )

    try:

        return (
            PolicyRAGResponse
            .model_validate(
                response.json()
            )
        )

    except (
        ValidationError,
        ValueError
    ):

        return PolicyRAGResponse(
            success=False,
            code="RAG_RESPONSE_INVALID",
            message=(
                "政策知识服务返回格式不合法"
            )
        )
