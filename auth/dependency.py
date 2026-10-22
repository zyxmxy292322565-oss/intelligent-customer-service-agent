"""FastAPI Bearer 认证依赖；在 OpenAPI 中声明受保护接口。"""

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth.jwt import decode_access_token


security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> int:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="缺少或无效的 Bearer Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
