from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse

from cache.limiter import TokenBucketLimiter
from core.settings import get_settings
from core.logging import log_extra
from auth.dependency import get_current_user, security

import logging
from redis.exceptions import RedisError


settings = get_settings()
logger = logging.getLogger("agent.rate_limit")
global_limiter = TokenBucketLimiter(
    capacity=settings.agent_global_capacity,
    rate=settings.agent_global_rate,
)


user_limiter = TokenBucketLimiter(
    capacity=settings.agent_user_capacity,
    rate=settings.agent_user_rate,
)





class RateLimitMiddleware(
    BaseHTTPMiddleware
):

    async def dispatch(
        self,
        request: Request,
        call_next
    ):

        path = request.url.path


        # 非 Agent 接口直接放行
        if not path.startswith("/agent"):
            return await call_next(request)


        # 在访问 Redis 前验证 Token；限流 key 不能来自可伪造的 user-id header。
        try:
            credentials = await security(request)
            user_id = get_current_user(credentials)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )

        # 全局限流
        try:
            global_allowed = global_limiter.allow(
                f"{settings.redis_key_prefix}:rate_limit:global"
            )
        except RedisError:
            logger.exception("Redis rate limiter unavailable", extra=log_extra())
            return JSONResponse(
                status_code=503,
                content={"code": "RATE_LIMIT_UNAVAILABLE", "message": "限流服务暂不可用"},
            )

        if not global_allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "系统繁忙，请稍后再试"
                },
                headers={
                    "Retry-After": "5"
                }
            )

        # 用户限流
        try:
            user_allowed = user_limiter.allow(
                f"{settings.redis_key_prefix}:rate_limit:user:{user_id}"
            )
        except RedisError:
            logger.exception("Redis rate limiter unavailable", extra=log_extra())
            return JSONResponse(
                status_code=503,
                content={"code": "RATE_LIMIT_UNAVAILABLE", "message": "限流服务暂不可用"},
            )

        if not user_allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "请求过于频繁，请稍后再试"
                },
                headers={
                    "Retry-After": "5"
                }
            )

        response = await call_next(
            request
        )

        return response
