"""Small HTTP client for the existing FastAPI endpoints."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, message: str, *, category: str = "unknown"):
        super().__init__(message)
        self.category = category


class BackendAPI:
    def __init__(self, token: str | None = None, base_url: str | None = None,
                 admin_key: str | None = None):
        self.token = token
        self.admin_key = admin_key
        self.base_url = (base_url or os.getenv("BACKEND_URL") or "http://localhost:8000").rstrip("/")

    def _request(self, method: str, path: str, *, timeout: int = 15, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if self.admin_key:
            headers["X-Admin-Key"] = self.admin_key
        url = f"{self.base_url}{path}"
        try:
            response = httpx.request(
                method, url, headers=headers, timeout=timeout, trust_env=False, **kwargs
            )
        except httpx.RequestError as exc:
            logger.error("Backend connection failed url=%s status=unavailable body=unavailable error=%s", url, exc)
            category = "timeout" if isinstance(exc, httpx.TimeoutException) else "connection"
            raise APIError(f"无法连接后端：{url}（{exc}）", category=category) from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text[:1000]
            logger.warning("Backend HTTP error url=%s status=%s body=%s", url, response.status_code, body)
            raise APIError(f"后端请求失败：{url}，HTTP {response.status_code}，响应：{body}", category="http") from exc

        try:
            return response.json()
        except ValueError as exc:
            body = response.text[:1000]
            logger.error("Backend invalid JSON url=%s status=%s body=%s", url, response.status_code, body)
            raise APIError(f"后端响应不是有效 JSON：{url}，HTTP {response.status_code}", category="invalid_response") from exc

    def login(self, user_id: int) -> str:
        result = self._request("POST", "/auth/login", json={"user_id": user_id})
        token = result.get("access_token")
        if not token:
            raise APIError("登录响应缺少 access_token")
        self.token = token
        return token

    def chat(self, message: str, conversation_id: int | None = None) -> dict:
        payload = {"message": message}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        return self._request("POST", "/agent/chat", json=payload, timeout=90)

    def conversations(self) -> dict:
        return self._request("GET", "/agent/conversations")

    def new_conversation(self) -> dict:
        return self._request("POST", "/agent/conversations")

    def conversation_messages(self, conversation_id: int) -> dict:
        return self._request("GET", f"/agent/conversations/{conversation_id}/messages")

    def handoff(self, conversation_id: int, reason: str = "用户请求人工客服") -> dict:
        return self._request(
            "POST", f"/agent/conversations/{conversation_id}/handoff", json={"reason": reason}
        )

    def confirm(self, confirmation_id: str, conversation_id: int | None = None) -> dict:
        payload = {"confirmation_id": confirmation_id}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        return self._request("POST", "/agent/confirm", json=payload, timeout=90)

    def after_sales(self, user_id: int) -> dict:
        return self._request("GET", f"/users/{user_id}/after-sales")

    def tasks(self, conversation_id: int | None = None) -> dict:
        params = {"conversation_id": conversation_id} if conversation_id is not None else None
        return self._request("GET", "/agent/tasks", params=params)

    def trace(self, task_id: int, conversation_id: int | None = None) -> dict:
        params = {"conversation_id": conversation_id} if conversation_id is not None else None
        return self._request("GET", f"/agent/trace/{task_id}", params=params)

    def admin_handoff_tasks(self, status: str = "waiting") -> dict:
        return self._request("GET", "/admin/handoff/tasks", params={"status": status})

    def admin_handoff_detail(self, handoff_id: int) -> dict:
        return self._request("GET", f"/admin/handoff/{handoff_id}")

    def admin_handoff_accept(self, handoff_id: int) -> dict:
        return self._request("POST", f"/admin/handoff/{handoff_id}/accept")

    def admin_handoff_complete(self, handoff_id: int) -> dict:
        return self._request("POST", f"/admin/handoff/{handoff_id}/complete")

    def admin_handoff_message(self, handoff_id: int, content: str) -> dict:
        return self._request(
            "POST", f"/admin/handoff/{handoff_id}/message", json={"content": content}
        )

    def admin_policy_documents(self) -> dict:
        return self._request("GET", "/admin/policy/documents")

    def admin_policy_upload(
        self,
        filename: str,
        content: bytes,
        content_type: str | None,
        category: str,
        product_scope: str,
        version: str,
    ) -> dict:
        return self._request(
            "POST",
            "/admin/policy/documents",
            files={"file": (filename, content, content_type or "application/octet-stream")},
            data={
                "category": category,
                "product_scope": product_scope,
                "version": version,
            },
            timeout=120,
        )

    def admin_policy_delete(self, document_id: int) -> dict:
        return self._request("DELETE", f"/admin/policy/documents/{document_id}", timeout=120)
