import time
from pathlib import Path

from cache.client import get_redis
from core.settings import get_settings


class TokenBucketLimiter:

    def __init__(
        self,
        capacity: int,
        rate: float,
        ttl_seconds: int | None = None
    ):
        self.redis = get_redis()
        self.capacity = capacity
        self.rate = rate
        self.ttl_seconds = ttl_seconds or get_settings().redis_rate_limit_ttl_seconds

        script_path = (
            Path(__file__).parent
            / "scripts"
            / "token_bucket.lua"
        )

        with open(
            script_path,
            "r",
            encoding="utf-8"
        ) as f:
            self.lua_script = f.read()


    def allow(
        self,
        key: str
    ) -> bool:

        now = int(time.time())

        result = self.redis.eval(
            self.lua_script,
            1,
            key,
            self.capacity,
            self.rate,
            now,
            self.ttl_seconds
        )

        return result == 1
