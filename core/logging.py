"""Agent 结构化文本日志；request_id 当前与 API 返回的 trace_id 相同。"""

import logging
import sys

from core.settings import get_settings


class AgentFormatter(logging.Formatter):
    def format(self, record):
        for field in ("request_id", "trace_id", "task_id", "confirmation_id"):
            if not hasattr(record, field):
                setattr(record, field, "-")
        return super().format(record)


def configure_logging():
    logger = logging.getLogger("agent")
    logger.setLevel(get_settings().log_level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(AgentFormatter(
            "%(asctime)s %(levelname)s %(name)s "
            "request_id=%(request_id)s trace_id=%(trace_id)s "
            "task_id=%(task_id)s confirmation_id=%(confirmation_id)s %(message)s"
        ))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_extra(*, trace_id=None, task_id=None, confirmation_id=None, request_id=None):
    return {
        "request_id": request_id or trace_id or "-",
        "trace_id": trace_id or "-",
        "task_id": task_id if task_id is not None else "-",
        "confirmation_id": confirmation_id or "-",
    }
