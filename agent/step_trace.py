"""Agent 步骤的附加审计记录；埋点失败不改变业务执行结果。"""

import json
import logging
from time import perf_counter

from db.connection import get_connection
from core.logging import log_extra

logger = logging.getLogger("agent.step_trace")


_SENSITIVE_KEYS = {"user_text", "reason", "phone", "address", "email", "password", "token"}


def _safe_data(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else _safe_data(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_data(item) for item in value]
    return value


def _json(value):
    return json.dumps(_safe_data(value), ensure_ascii=False, default=str)


def _start_step(task_id, trace_id, step_name, input_data):
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO agent_task_steps
                (task_id, trace_id, step_name, status, input_json)
            VALUES (%s, %s, %s, 'START', %s)
        """, (task_id, trace_id, step_name, _json(input_data)))
        step_id = cursor.lastrowid
        connection.commit()
        return step_id
    except Exception:
        connection.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()


def _finish_step(step_id, status, output_data, error_message, duration_ms):
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("""
            UPDATE agent_task_steps
            SET status=%s, output_json=%s, error_message=%s, latency_ms=%s
            WHERE id=%s
        """, (status, _json(output_data), error_message, duration_ms, step_id))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()


class trace_step:
    """START -> SUCCESS/FAILED；业务失败调用 set_result，异常自动记录后原样抛出。"""

    def __init__(self, task_id, trace_id, step_name, input_data=None):
        self.task_id = task_id
        self.trace_id = trace_id
        self.step_name = step_name
        self.input_data = input_data
        self.output_data = None
        self.status = "SUCCESS"
        self.error_message = None
        self.step_id = None

    def __enter__(self):
        self.started = perf_counter()
        try:
            self.step_id = _start_step(
                self.task_id, self.trace_id, self.step_name, self.input_data
            )
        except Exception:
            logger.exception("Agent step trace START failed: %s", self.step_name,
                             extra=log_extra(trace_id=self.trace_id, task_id=self.task_id))
        return self

    def set_result(self, result):
        self.output_data = result
        if isinstance(result, dict) and result.get("success") is False:
            self.status = "FAILED"
            self.error_message = str(result.get("message") or result.get("code") or "business failure")

    def __exit__(self, exc_type, exc, traceback):
        duration_ms = round((perf_counter() - self.started) * 1000, 3)
        if exc is not None:
            self.status = "FAILED"
            self.error_message = f"{type(exc).__name__}: {exc}"
        if self.step_id is not None:
            try:
                _finish_step(
                    self.step_id, self.status, self.output_data,
                    self.error_message, duration_ms
                )
            except Exception:
                logger.exception("Agent step trace finish failed: %s", self.step_name,
                                 extra=log_extra(trace_id=self.trace_id, task_id=self.task_id))
        return False
