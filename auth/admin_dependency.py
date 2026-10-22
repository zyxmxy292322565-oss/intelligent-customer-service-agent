"""Separate administrator credential; customer JWTs never grant workspace access."""

import hmac

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from core.settings import get_settings


admin_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def require_admin_key(provided: str | None = Depends(admin_header)) -> None:
    configured = get_settings().admin_api_key
    if not configured:
        raise HTTPException(status_code=503, detail="客服工作台尚未配置")
    if not provided:
        raise HTTPException(status_code=401, detail="缺少客服工作台凭证")
    if not hmac.compare_digest(provided, configured):
        raise HTTPException(status_code=403, detail="无权访问客服工作台")
