"""Wall-clock budget for read-only LLM analysis, independent of HTTP timeouts."""

import logging
import queue
import threading
from time import monotonic

import httpx
import openai
from pydantic import ValidationError

from core.logging import log_extra

logger = logging.getLogger("agent.input_analyzer")
INPUT_ANALYSIS_BUDGET_SECONDS = 40.0
INPUT_ANALYSIS_STAGE_SECONDS = 20.0
# Timed-out synchronous calls cannot be killed safely. Bound their number and
# fail closed under saturation; workers never receive database operations.
_slots = threading.BoundedSemaphore(8)


class InputAnalysisError(Exception):
    def __init__(self, stage, category):
        self.stage = stage
        self.category = category
        super().__init__(f"input_analysis stage={stage} category={category}")

    def as_result(self):
        timeout = self.category in {"http_timeout", "stage_timeout", "analysis_timeout"}
        return {
            "success": False,
            "code": "INPUT_ANALYSIS_TIMEOUT" if timeout else "INPUT_ANALYSIS_UNAVAILABLE",
            "message": "理解您的问题时超时，请稍后重试。" if timeout else "暂时无法理解您的问题，请稍后重试。",
            "data": None,
        }


def run_analysis_stage(stage, func, text, deadline, *, trace_id=None, task_id=None, expected_type=None):
    start = monotonic()
    extra = log_extra(trace_id=trace_id, task_id=task_id)
    logger.info("input stage=%s status=start", stage, extra=extra)
    exception_type = "InputAnalysisError"
    remaining = deadline - start
    if remaining <= 0:
        error = InputAnalysisError(stage, "analysis_timeout")
    elif not _slots.acquire(blocking=False):
        error = InputAnalysisError(stage, "capacity_exhausted")
    else:
        results = queue.Queue(maxsize=1)

        def worker():
            try:
                results.put((True, func(text)))
            except Exception as exc:
                results.put((False, exc))
            finally:
                _slots.release()

        thread = threading.Thread(target=worker, daemon=True, name="agent-input-llm")
        try:
            thread.start()
        except Exception:
            _slots.release()
            raise
        try:
            ok, value = results.get(timeout=min(INPUT_ANALYSIS_STAGE_SECONDS, remaining))
        except queue.Empty:
            error = InputAnalysisError(stage, "analysis_timeout" if remaining <= INPUT_ANALYSIS_STAGE_SECONDS else "stage_timeout")
        else:
            if ok and expected_type is not None and not isinstance(value, expected_type):
                ok, value = False, TypeError("invalid structured result")
            if ok:
                logger.info("input stage=%s status=success elapsed_ms=%.1f", stage, (monotonic()-start)*1000, extra=extra)
                return value
            if isinstance(value, (openai.APITimeoutError, httpx.TimeoutException, TimeoutError)):
                category = "http_timeout"
            elif isinstance(value, (ValidationError, ValueError, TypeError)):
                category = "invalid_response"
            else:
                category = "upstream_error"
            exception_type = type(value).__name__
            error = InputAnalysisError(stage, category)
    logger.warning("input stage=%s status=failed elapsed_ms=%.1f category=%s exception_type=%s", stage, (monotonic()-start)*1000, error.category, exception_type, extra=extra)
    raise error
