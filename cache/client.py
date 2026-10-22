import redis
from core.settings import get_settings


_settings = get_settings()
REDIS_HOST = _settings.redis_host
REDIS_PORT = _settings.redis_port


redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=_settings.redis_db,
    password=_settings.redis_password,
    socket_connect_timeout=_settings.redis_socket_timeout,
    socket_timeout=_settings.redis_socket_timeout,
    decode_responses=True
)


def get_redis():
    return redis_client


def check_redis():
    return redis_client.ping()
