"""P3 infrastructure checks; no live Redis or business database required."""

import logging
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

import main
from auth.jwt import create_access_token
from cache.limiter import TokenBucketLimiter
from core.logging import AgentFormatter, log_extra
from core.settings import get_settings


class SettingsTests(unittest.TestCase):
    def tearDown(self):
        get_settings.cache_clear()

    def test_langgraph_flag_defaults_off_and_accepts_true(self):
        with patch.dict(os.environ, {"USE_LANGGRAPH_AGENT": "false"}):
            get_settings.cache_clear()
            self.assertFalse(get_settings().use_langgraph_agent)
        with patch.dict(os.environ, {"USE_LANGGRAPH_AGENT": "true"}):
            get_settings.cache_clear()
            self.assertTrue(get_settings().use_langgraph_agent)

    def test_environment_overrides_dotenv_defaults(self):
        with patch.dict(os.environ, {"APP_ENV": "development", "MYSQL_HOST": "ci-mysql"}):
            get_settings.cache_clear()
            self.assertEqual(get_settings().mysql_host, "ci-mysql")

    def test_production_rejects_missing_credentials(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production", "MYSQL_PASSWORD": "", "DEEPSEEK_API_KEY": ""
        }):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "生产环境"):
                get_settings()

    def test_production_requires_jwt_signing_secret(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production", "MYSQL_PASSWORD": "db-secret",
            "DEEPSEEK_API_KEY": "llm-key", "JWT_SECRET_KEY": "",
        }):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "JWT_SECRET_KEY"):
                get_settings()

    def test_legacy_llm_config_remains_supported(self):
        with patch.dict(os.environ, {
            "APP_ENV": "test", "DEEPSEEK_BASE_URL": "", "DEEPSEEK_API_BASE": "https://legacy.example",
            "DEEPSEEK_MODEL": "", "MODEL": "legacy-model",
        }):
            get_settings.cache_clear()
            settings = get_settings()
            self.assertEqual(settings.deepseek_base_url, "https://legacy.example")
            self.assertEqual(settings.deepseek_model, "legacy-model")

    def test_ttl_cannot_reset_bucket_before_full_refill(self):
        with patch.dict(os.environ, {
            "APP_ENV": "test", "REDIS_RATE_LIMIT_TTL_SECONDS": "2",
            "AGENT_GLOBAL_CAPACITY": "100", "AGENT_GLOBAL_RATE": "1",
        }):
            get_settings.cache_clear()
            with self.assertRaisesRegex(ValueError, "TTL"):
                get_settings()


class LoggingTests(unittest.TestCase):
    def test_log_fields_include_request_trace_task_and_confirmation(self):
        record = logging.LogRecord("agent", logging.INFO, __file__, 1, "started", (), None)
        for name, value in log_extra(trace_id="trace-1", task_id=42, confirmation_id="CONF_1").items():
            setattr(record, name, value)
        output = AgentFormatter(
            "%(levelname)s %(request_id)s %(trace_id)s %(task_id)s %(confirmation_id)s"
        ).format(record)
        self.assertEqual(output, "INFO trace-1 trace-1 42 CONF_1")


class RedisLimiterTests(unittest.TestCase):
    def test_limiter_passes_ttl_to_atomic_script(self):
        with patch("cache.limiter.get_redis") as get_redis:
            get_redis.return_value.eval.return_value = 1
            limiter = TokenBucketLimiter(capacity=5, rate=1, ttl_seconds=123)
            self.assertTrue(limiter.allow("consumer_agent:rate_limit:user:1"))
            args = get_redis.return_value.eval.call_args.args
            self.assertEqual(args[2], "consumer_agent:rate_limit:user:1")
            self.assertEqual(args[-1], 123)
            self.assertIn('redis.call("EXPIRE", KEYS[1], ttl)', limiter.lua_script)

    def test_redis_failure_returns_503_without_running_agent(self):
        with patch("middleware.rate_limit.global_limiter.allow", side_effect=RedisConnectionError("down")):
            with TestClient(main.app) as client:
                result = client.post("/agent/chat", json={"user_id": 1, "message": "hello"},
                                     headers={"Authorization": f"Bearer {create_access_token(1)}"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["code"], "RATE_LIMIT_UNAVAILABLE")

    def test_agent_route_uses_namespaced_limit_keys(self):
        with patch("middleware.rate_limit.global_limiter.allow", return_value=True) as global_allow, \
             patch("middleware.rate_limit.user_limiter.allow", return_value=True) as user_allow:
            with TestClient(main.app) as client:
                result = client.post("/agent/test", headers={
                    "Authorization": f"Bearer {create_access_token(7)}", "user-id": "999"
                })
        self.assertEqual(result.status_code, 200)
        self.assertTrue(global_allow.call_args.args[0].endswith(":rate_limit:global"))
        self.assertTrue(user_allow.call_args.args[0].endswith(":rate_limit:user:7"))


class HealthTests(unittest.TestCase):
    def test_ready_when_mysql_and_redis_respond(self):
        connection = MagicMock()
        with patch("main.get_connection", return_value=connection), patch("main.check_redis", return_value=True):
            with TestClient(main.app) as client:
                result = client.get("/ready")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"status": "ready"})
        connection.close.assert_called_once()

    def test_not_ready_when_dependency_fails(self):
        with patch("main.get_connection", side_effect=RuntimeError("database down")):
            with TestClient(main.app) as client:
                result = client.get("/ready")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json(), {"status": "unavailable"})


if __name__ == "__main__":
    unittest.main()
