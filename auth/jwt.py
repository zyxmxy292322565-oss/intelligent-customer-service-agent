"""JWT 签发与校验；算法、签发者和受众由服务端固定。"""

from datetime import datetime, timedelta, timezone

import jwt

from core.settings import get_settings


ALGORITHM = "HS256"
ISSUER = "consumer-service-agent"
AUDIENCE = "consumer-service-agent-api"


def create_access_token(user_id: int, *, expires_delta: timedelta | None = None) -> str:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("user_id 必须是正整数")
    now = datetime.now(timezone.utc)
    settings = get_settings()
    expires = (
        expires_delta if expires_delta is not None
        else timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + expires,
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=ALGORITHM)

def decode_access_token(token: str) -> int:
    payload = jwt.decode(
        token,
        get_settings().jwt_secret_key,
        algorithms=[ALGORITHM],
        issuer=ISSUER,
        audience=AUDIENCE,
        options={"require": ["sub", "iat", "exp", "iss", "aud"]},
    )
    subject = payload["sub"]
    if not isinstance(subject, str) or not subject.isdecimal() or int(subject) <= 0:
        raise jwt.InvalidTokenError("无效的用户标识")
    return int(subject)
