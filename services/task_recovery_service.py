"""Bounded recovery for expired database-owned Agent task executions.

Recovery never replays Agent, RAG, Preview, Confirm, or business mutations.  It
only fences an expired owner and coordinates already-persisted evidence.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Callable, TypeVar

import pymysql

from db.connection import get_connection
from services.after_sale_operation_service import (
    OperationResultUncertain,
    get_operation_receipt,
)


logger = logging.getLogger("services.task_recovery")
T = TypeVar("T")
_TRANSIENT_DB_ERRORS = (pymysql.err.OperationalError, pymysql.err.InterfaceError)


def new_task_recovery_id() -> str:
    return "REC_" + uuid.uuid4().hex.upper()


def _with_db_retry(operation: Callable[[], T], attempts: int = 3) -> T:
    """Retry only transient connection errors; never replay business work."""
    last_error: BaseException | None = None
    started = time.monotonic()
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return operation()
        except _TRANSIENT_DB_ERRORS as exc:
            last_error = exc
            if (isinstance(exc, pymysql.err.OperationalError)
                    and (not exc.args or exc.args[0] not in {1205,1213,2002,2003,2006,2013})):
                raise
            if attempt >= attempts or time.monotonic() - started + 0.05 * attempt >= 5:
                raise
            time.sleep(0.05 * attempt)
    assert last_error is not None
    raise last_error


def list_task_recovery_candidates(
    lease_seconds: int,
    recovery_claim_seconds: int,
    batch_size: int,
) -> list[dict]:
    """Oldest due first, using durable timestamps rather than an ID cursor.

    Successful claims advance recovery_started_at atomically, moving recurring
    review work behind older unserved candidates. Restarted/concurrent workers
    share this ordering; claim_task_recovery remains the ownership arbiter.
    """
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, execution_id, recovery_id, status
                FROM agent_tasks
                WHERE (
                    status='running'
                    AND execution_id IS NOT NULL
                    AND recovery_id IS NULL
                    AND execution_heartbeat_at IS NOT NULL
                    AND execution_heartbeat_at <= DATE_SUB(NOW(), INTERVAL %s SECOND)
                ) OR (
                    status IN ('recovering', 'recovery_review')
                    AND recovery_id IS NOT NULL
                    AND recovery_started_at <= DATE_SUB(NOW(), INTERVAL %s SECOND)
                )
                ORDER BY CASE WHEN status='running'
                    THEN DATE_ADD(execution_heartbeat_at, INTERVAL %s SECOND)
                    ELSE DATE_ADD(recovery_started_at, INTERVAL %s SECOND)
                END ASC, id ASC
                LIMIT %s
            """, (lease_seconds, recovery_claim_seconds,
                  lease_seconds, recovery_claim_seconds, max(1, batch_size)))
            return cursor.fetchall()
    finally:
        connection.close()


def claim_task_recovery(
    candidate: dict,
    recovery_id: str,
    lease_seconds: int = 60,
    recovery_claim_seconds: int = 60,
) -> bool:
    """Atomically rotate execution ownership from an expired worker to recovery."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            if candidate["status"] == "running":
                cursor.execute("""
                    UPDATE agent_tasks
                    SET recovery_source_execution_id=execution_id,
                        recovery_id=%s,
                        recovery_started_at=NOW(),
                        recovery_status='processing',
                        recovery_reason=NULL,
                        execution_id=%s,
                        execution_heartbeat_at=NULL,
                        status='recovering',
                        current_step='task_recovery'
                    WHERE id=%s AND status='running'
                      AND execution_id=%s AND recovery_id IS NULL
                      AND execution_heartbeat_at IS NOT NULL
                      AND execution_heartbeat_at <= DATE_SUB(NOW(), INTERVAL %s SECOND)
                """, (
                    recovery_id, recovery_id,
                    candidate["id"], candidate["execution_id"], lease_seconds,
                ))
            else:
                cursor.execute("""
                    UPDATE agent_tasks
                    SET recovery_id=%s, recovery_started_at=NOW(),
                        recovery_status='processing', recovery_reason=NULL,
                        execution_id=%s, execution_heartbeat_at=NULL,
                        current_step='task_recovery', status='recovering'
                    WHERE id=%s AND status IN ('recovering','recovery_review')
                      AND recovery_id=%s AND execution_id=%s
                      AND recovery_started_at <= DATE_SUB(NOW(), INTERVAL %s SECOND)
                """, (
                    recovery_id, recovery_id, candidate["id"],
                    candidate["recovery_id"], candidate["execution_id"],
                    recovery_claim_seconds,
                ))
            claimed = cursor.rowcount == 1
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _recovery_step(cursor, task_id: int, recovery_id: str, outcome: str, reason: str) -> None:
    cursor.execute("""
        INSERT INTO agent_task_steps
            (task_id, trace_id, step_name, status, output_json, error_message, latency_ms)
        VALUES (%s, %s, 'task_recovery', %s, %s, %s, 0)
    """, (
        task_id,
        recovery_id,
        "FAILED" if outcome in {"failed", "review_required"} else "SUCCESS",
        _json({"recovery_id": recovery_id, "outcome": outcome}),
        reason if outcome in {"failed", "review_required"} else None,
    ))


def _create_after_sale_evidence(cursor, action: dict, preview: dict) -> dict | None:
    if not preview.get('order_item_id') or not preview.get('after_sale_type'):
        return None
    cursor.execute("""
        SELECT id, after_sale_no, user_id, order_id, order_item_id, type, status,
               refund_amount, idempotency_key
        FROM after_sales
        WHERE idempotency_key=%s AND user_id=%s
        LIMIT 1
    """, (action["confirmation_id"], action["user_id"]))
    row = cursor.fetchone()
    if row is None:
        return None
    if (
        preview.get("order_item_id") is not None
        and int(preview["order_item_id"]) != int(row["order_item_id"])
    ):
        return None
    if preview.get("after_sale_type") and preview["after_sale_type"] != row["type"]:
        return None
    return {
        "success": True,
        "code": "IDEMPOTENT_REPLAY",
        "message": "已核实原售后申请写入结果",
        "data": row,
    }


def _operation_receipt_evidence(action: dict, preview: dict) -> dict | None:
    try:
        return get_operation_receipt(
            action["confirmation_id"],
            action["user_id"],
            action["action_type"],
            preview,
        )
    except (OperationResultUncertain, ValueError):
        return None


def _finalize_recovery(
    cursor,
    task: dict,
    recovery_id: str,
    *,
    task_status: str,
    recovery_status: str,
    reason: str,
    action: dict | None = None,
    action_result: dict | None = None,
) -> None:
    if action is not None and action_result is not None:
        cursor.execute("""
            UPDATE agent_pending_actions
            SET status='executed', execution_result_json=%s,
                error_message=NULL, executed_at=COALESCE(executed_at, NOW())
            WHERE id=%s AND status='executing'
        """, (_json(action_result), action["id"]))
        if cursor.rowcount != 1:
            raise RuntimeError("Action changed while recovery was finalizing")

    releases_owner = task_status in {
        "failed", "completed", "waiting_confirm", "waiting_input"
    }
    cursor.execute("""
        UPDATE agent_tasks
        SET status=%s,
            current_step=%s,
            execution_id=CASE WHEN %s THEN NULL ELSE execution_id END,
            execution_started_at=CASE WHEN %s THEN NULL ELSE execution_started_at END,
            execution_heartbeat_at=NULL,
            recovery_status=%s,
            recovery_reason=%s
        WHERE id=%s AND status='recovering'
          AND recovery_id=%s AND execution_id=%s
    """, (
        task_status,
        "recovery_review" if task_status == "recovery_review" else task_status,
        releases_owner,
        releases_owner,
        recovery_status,
        reason,
        task["id"],
        recovery_id,
        recovery_id,
    ))
    if cursor.rowcount != 1:
        raise RuntimeError("Task recovery ownership was lost")
    _recovery_step(cursor, task["id"], recovery_id, recovery_status, reason)


def recover_claimed_task(task_id: int, recovery_id: str) -> str:
    """Converge from durable evidence without replaying any business operation."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT * FROM agent_tasks
                WHERE id=%s AND status='recovering'
                  AND recovery_id=%s AND execution_id=%s
                FOR UPDATE
            """, (task_id, recovery_id, recovery_id))
            task = cursor.fetchone()
            if task is None:
                connection.rollback()
                return "skipped"
            cursor.execute("""
                SELECT * FROM agent_pending_actions
                WHERE task_id=%s AND user_id=%s
                ORDER BY id DESC LIMIT 1
                FOR UPDATE
            """, (task_id, task["user_id"]))
            action = cursor.fetchone()

            if action is None:
                _finalize_recovery(
                    cursor, task, recovery_id,
                    task_status="failed", recovery_status="failed",
                    reason="LEASE_EXPIRED_NO_BUSINESS_SIDE_EFFECT",
                )
                connection.commit()
                return "failed"

            preview = action["preview_json"]
            if isinstance(preview, str):
                preview = json.loads(preview)

            if action["status"] == "pending":
                _finalize_recovery(
                    cursor, task, recovery_id,
                    task_status="waiting_confirm",
                    recovery_status="waiting_confirm",
                    reason="PENDING_ACTION_PRESERVED",
                )
                connection.commit()
                return "waiting_confirm"

            if action["status"] in {"executed", "failed"}:
                terminal = "completed" if action["status"] == "executed" else "failed"
                _finalize_recovery(
                    cursor, task, recovery_id,
                    task_status=terminal, recovery_status=terminal,
                    reason="ACTION_TERMINAL_STATE_VERIFIED",
                )
                connection.commit()
                return terminal

            evidence = None
            if action["status"] == "executing":
                if action["action_type"] == "create_after_sale":
                    evidence = _create_after_sale_evidence(cursor, action, preview)
                elif action["action_type"] in {
                    "cancel_after_sale", "change_after_sale_type"
                }:
                    evidence = _operation_receipt_evidence(action, preview)

            if evidence is not None:
                _finalize_recovery(
                    cursor, task, recovery_id,
                    task_status="completed", recovery_status="completed",
                    reason="BUSINESS_RESULT_CREDENTIAL_VERIFIED",
                    action=action, action_result=evidence,
                )
                connection.commit()
                return "completed"

            _finalize_recovery(
                cursor, task, recovery_id,
                task_status="recovery_review",
                recovery_status="review_required",
                reason="BUSINESS_RESULT_UNKNOWN",
            )
            connection.commit()
            return "review_required"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def recover_expired_agent_tasks(
    *,
    lease_seconds: int,
    recovery_claim_seconds: int,
    batch_size: int,
    db_attempts: int = 3,
) -> dict:
    """Run one bounded scan.  One bad record never aborts later candidates."""
    summary = {
        "scanned": 0, "claimed": 0, "failed": 0,
        "waiting_confirm": 0, "completed": 0,
        "review_required": 0, "skipped": 0, "errors": 0,
    }
    candidates = _with_db_retry(
        lambda: list_task_recovery_candidates(
            lease_seconds, recovery_claim_seconds, batch_size,
        ),
        db_attempts,
    )
    summary["scanned"] = len(candidates)
    for candidate in candidates:
        recovery_id = new_task_recovery_id()
        try:
            claimed = _with_db_retry(
                lambda c=candidate, rid=recovery_id: claim_task_recovery(
                    c, rid, lease_seconds, recovery_claim_seconds,
                ),
                db_attempts,
            )
            if not claimed:
                summary["skipped"] += 1
                continue
            summary["claimed"] += 1
            result = _with_db_retry(
                lambda tid=candidate["id"], rid=recovery_id: recover_claimed_task(tid, rid),
                db_attempts,
            )
            summary[result] = summary.get(result, 0) + 1
            logger.warning(
                "task recovery finished task_id=%s recovery_id=%s outcome=%s",
                candidate["id"], recovery_id, result,
            )
        except Exception:
            summary["errors"] += 1
            logger.exception(
                "task recovery failed task_id=%s recovery_id=%s",
                candidate["id"], recovery_id,
            )
    return summary
