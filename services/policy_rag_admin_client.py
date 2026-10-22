"""Backend-only client for Policy RAG knowledge-base administration.

The Streamlit administration workbench must call the backend, never the
internal ``policy-rag`` container directly.  This module keeps that boundary
small while preserving the existing Policy RAG upload/query protocols.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.settings import get_settings


@dataclass
class PolicyRAGAdminError(Exception):
    """An expected error from the internal Policy RAG management API."""

    status_code: int
    detail: str


def _base_url() -> str:
    return get_settings().policy_rag_service_url.rstrip("/")


def _request(method: str, path: str, *, timeout: float = 30, **kwargs) -> dict:
    url = f"{_base_url()}{path}"
    try:
        response = httpx.request(method, url, timeout=timeout, trust_env=False, **kwargs)
    except httpx.TimeoutException as exc:
        raise PolicyRAGAdminError(504, "政策知识服务响应超时") from exc
    except httpx.RequestError as exc:
        raise PolicyRAGAdminError(503, "政策知识服务暂时不可用") from exc

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise PolicyRAGAdminError(502, "政策知识服务管理请求失败") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise PolicyRAGAdminError(502, "政策知识服务返回格式不合法") from exc
    if not isinstance(payload, dict):
        raise PolicyRAGAdminError(502, "政策知识服务返回格式不合法")
    return payload


def _require_success(payload: dict, *, not_found: bool = False) -> dict:
    if payload.get("success", True):
        return payload
    status_code = 404 if not_found else 422
    raise PolicyRAGAdminError(status_code, str(payload.get("message") or "政策文档操作失败"))


def upload_policy_document(
    *,
    filename: str,
    content: bytes,
    content_type: str | None,
    category: str,
    product_scope: str,
    version: str,
    created_by: str,
) -> dict:
    payload = _request(
        "POST",
        "/upload",
        timeout=120,
        files={"file": (filename, content, content_type or "application/octet-stream")},
        data={
            "category": category,
            "product_scope": product_scope,
            "version": version,
            "created_by": created_by,
        },
    )
    return _require_success(payload)


def list_policy_documents() -> list[dict]:
    payload = _request("GET", "/documents")
    documents = payload.get("data")
    if not isinstance(documents, list):
        raise PolicyRAGAdminError(502, "政策知识服务文档列表格式不合法")
    return documents


def delete_policy_document(document_id: int) -> dict:
    documents = list_policy_documents()
    document = None
    for row in documents:
        try:
            matches_id = int(row.get("ID")) == document_id
        except (TypeError, ValueError):
            matches_id = False
        if matches_id:
            document = row
            break
    if document is None:
        raise PolicyRAGAdminError(404, "政策文档不存在")
    file_hash = document.get("文件hash")
    if not isinstance(file_hash, str) or not file_hash:
        raise PolicyRAGAdminError(502, "政策文档缺少删除标识")
    payload = _request("DELETE", f"/documents/{file_hash}", timeout=120)
    return _require_success(payload, not_found=True)
