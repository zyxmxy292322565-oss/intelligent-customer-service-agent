"""统一加载本地 .env / 容器环境变量；生产环境缺少关键配置时尽早失败。"""

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    app_env: str
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    mysql_connect_timeout: int
    redis_host: str
    redis_port: int
    redis_db: int
    redis_password: str | None
    redis_socket_timeout: float
    redis_key_prefix: str
    redis_rate_limit_ttl_seconds: int
    agent_global_capacity: int
    agent_global_rate: float
    agent_user_capacity: int
    agent_user_rate: float
    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str
    use_langgraph_agent: bool
    policy_rag_service_url: str
    policy_rag_timeout_seconds: float
    log_level: str
    jwt_secret_key: str
    jwt_access_token_expire_minutes: int
    admin_api_key: str | None
    task_lease_seconds: int
    task_heartbeat_interval_seconds: int
    task_recovery_scan_interval_seconds: int
    task_recovery_batch_size: int
    task_recovery_claim_seconds: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    app_env = os.getenv("APP_ENV", "development").lower()
    if app_env not in {"development", "production", "test"}:
        raise ValueError("APP_ENV 必须为 development、production 或 test")

    graph_flag = os.getenv("USE_LANGGRAPH_AGENT", "false").strip().lower()
    if graph_flag not in {"true", "false"}:
        raise ValueError("USE_LANGGRAPH_AGENT 必须为 true 或 false")

    settings = Settings(
        app_env=app_env,
        mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.getenv("MYSQL_PORT", "3306")),
        mysql_user=os.getenv("MYSQL_USER", "root"),
        mysql_password=os.getenv("MYSQL_PASSWORD", ""),
        mysql_database=os.getenv("MYSQL_DATABASE", "consumer_service_agent"),
        mysql_connect_timeout=int(os.getenv("MYSQL_CONNECT_TIMEOUT", "3")),
        redis_host=os.getenv("REDIS_HOST", "127.0.0.1"),
        redis_port=int(os.getenv("REDIS_PORT", "6379")),
        redis_db=int(os.getenv("REDIS_DB", "0")),
        redis_password=os.getenv("REDIS_PASSWORD") or None,
        redis_socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "2")),
        redis_key_prefix=os.getenv("REDIS_KEY_PREFIX", "consumer_agent").strip(":"),
        redis_rate_limit_ttl_seconds=int(os.getenv("REDIS_RATE_LIMIT_TTL_SECONDS", "600")),
        agent_global_capacity=int(os.getenv("AGENT_GLOBAL_CAPACITY", "100")),
        agent_global_rate=float(os.getenv("AGENT_GLOBAL_RATE", "20")),
        agent_user_capacity=int(os.getenv("AGENT_USER_CAPACITY", "5")),
        agent_user_rate=float(os.getenv("AGENT_USER_RATE", "1")),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
        deepseek_base_url=(
            os.getenv("DEEPSEEK_BASE_URL")
            or os.getenv("DEEPSEEK_API_BASE")
            or "https://api.deepseek.com"
        ),
        deepseek_model=os.getenv("DEEPSEEK_MODEL") or os.getenv("MODEL") or "deepseek-chat",
        use_langgraph_agent=(graph_flag == "true"),
        policy_rag_service_url=os.getenv("POLICY_RAG_SERVICE_URL", "http://127.0.0.1:8001"),
        policy_rag_timeout_seconds=float(os.getenv("POLICY_RAG_TIMEOUT_SECONDS", "8")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        jwt_secret_key=os.getenv("JWT_SECRET_KEY") or secrets.token_urlsafe(32),
        jwt_access_token_expire_minutes=int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30")),
        admin_api_key=os.getenv("ADMIN_API_KEY") or None,
        task_lease_seconds=int(os.getenv("TASK_LEASE_SECONDS", "60")),
        task_heartbeat_interval_seconds=int(
            os.getenv("TASK_HEARTBEAT_INTERVAL_SECONDS", "10")
        ),
        task_recovery_scan_interval_seconds=int(
            os.getenv("TASK_RECOVERY_SCAN_INTERVAL_SECONDS", "30")
        ),
        task_recovery_batch_size=int(os.getenv("TASK_RECOVERY_BATCH_SIZE", "20")),
        task_recovery_claim_seconds=int(
            os.getenv("TASK_RECOVERY_CLAIM_SECONDS", "60")
        ),
    )
    if settings.app_env == "production" and (
        not settings.mysql_password or not settings.deepseek_api_key
    ):
        raise ValueError("生产环境必须设置 MYSQL_PASSWORD 和 DEEPSEEK_API_KEY")
    if settings.app_env == "production" and len(os.getenv("JWT_SECRET_KEY", "")) < 32:
        raise ValueError("生产环境必须设置至少 32 字符的 JWT_SECRET_KEY")
    if settings.jwt_access_token_expire_minutes <= 0:
        raise ValueError("JWT_ACCESS_TOKEN_EXPIRE_MINUTES 必须大于 0")
    if settings.app_env == "production" and settings.admin_api_key and len(settings.admin_api_key) < 32:
        raise ValueError("生产环境 ADMIN_API_KEY 至少需要 32 字符")
    if settings.redis_rate_limit_ttl_seconds <= 0:
        raise ValueError("REDIS_RATE_LIMIT_TTL_SECONDS 必须大于 0")
    if min(settings.agent_global_capacity, settings.agent_user_capacity) <= 0:
        raise ValueError("限流容量必须大于 0")
    if min(settings.agent_global_rate, settings.agent_user_rate) <= 0:
        raise ValueError("限流速率必须大于 0")
    if settings.redis_rate_limit_ttl_seconds < max(
        settings.agent_global_capacity / settings.agent_global_rate,
        settings.agent_user_capacity / settings.agent_user_rate,
    ):
        raise ValueError("限流 key TTL 不得短于令牌桶恢复至满额所需时间")
    if not settings.redis_key_prefix:
        raise ValueError("REDIS_KEY_PREFIX 不能为空")
    if min(
        settings.task_lease_seconds,
        settings.task_heartbeat_interval_seconds,
        settings.task_recovery_scan_interval_seconds,
        settings.task_recovery_batch_size,
        settings.task_recovery_claim_seconds,
    ) <= 0:
        raise ValueError("Task 租约与恢复参数必须大于 0")
    if settings.task_heartbeat_interval_seconds * 2 >= settings.task_lease_seconds:
        raise ValueError("Task 心跳间隔必须显著短于执行租约")
    return settings
