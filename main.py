import asyncio
import logging
import re
import pymysql
from core.database_errors import database_error_response
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from core.logging import configure_logging, log_extra
from db.connection import get_connection
from cache.client import check_redis
from core.settings import get_settings
from auth.dependency import get_current_user
from auth.admin_dependency import require_admin_key
from auth.jwt import create_access_token
from auth.schemas import LoginRequest
from db.repository import get_user_by_id, get_after_sale_by_id

from schemas.after_sale_schema import CreateAfterSaleRequest
from services.order_service import (
    get_user_orders_service,
    get_order_detail_service
)
from services.after_sale_service import create_after_sale_service

from schemas.after_sale_schema import (
    CreateAfterSaleRequest,
    UpdateAfterSaleStatusRequest
)

from services.after_sale_service import (
    create_after_sale_service,
    update_after_sale_status_service,
    get_user_after_sales_service,
    get_after_sale_detail_service,
    get_after_sale_history_service
)
from services.trace_service import get_user_task_trace, list_user_tasks
from services.conversation_service import (
    ConversationNotFound, create_conversation, create_handoff,
    get_conversation, get_conversation_messages, list_conversations, save_message,
)
from services.handoff_admin_service import (
    HandoffNotFound, HandoffStateConflict, get_handoff_detail,
    list_handoff_tasks, send_handoff_message, transition_handoff,
)
from services.policy_rag_admin_client import (
    PolicyRAGAdminError,
    delete_policy_document,
    list_policy_documents,
    upload_policy_document,
)
from services.conversation_realtime import conversation_hub
from services.conversation_websocket import router as conversation_websocket_router
from pydantic import BaseModel, Field

from middleware.rate_limit import RateLimitMiddleware

from schemas.agent_schema import AgentChatRequest

from agent.runtime import run_agent
from agent.after_sale_operations import detect_after_sale_operation, prepare_after_sale_operation

from schemas.agent_schema import (
    AgentChatRequest,
    AgentResponse,
    AgentConfirmRequest
)


from agent.confirmation_service import (
    confirm_action,
    cancel_action,
    expire_pending_actions,
)
from agent.confirmation_store import get_pending_action
from agent.step_trace import trace_step
from agent.conversation_scope import conversation_scope

from agent.action_executor import (
    execute_confirmed_action
)
from services.task_service import renew_registered_task_leases
from services.task_recovery_service import recover_expired_agent_tasks

logger = configure_logging()


async def _expire_pending_actions_periodically():
    while True:
        await asyncio.sleep(60 * 60)
        try:
            await asyncio.to_thread(expire_pending_actions)
        except Exception:
            logger.exception("pending action expiration failed")


async def _renew_task_leases_periodically():
    settings = get_settings()
    batch_size = getattr(settings, "task_recovery_batch_size", 20)
    interval = getattr(settings, "task_heartbeat_interval_seconds", 10)
    while True:
        try:
            await asyncio.to_thread(
                renew_registered_task_leases,
                max(100, batch_size),
            )
        except Exception:
            logger.exception("task lease heartbeat failed")
        await asyncio.sleep(interval)


async def _recover_tasks_periodically():
    settings = get_settings()
    scan_interval = getattr(settings, "task_recovery_scan_interval_seconds", 30)
    lease_seconds = getattr(settings, "task_lease_seconds", 60)
    claim_seconds = getattr(settings, "task_recovery_claim_seconds", 60)
    batch_size = getattr(settings, "task_recovery_batch_size", 20)
    while True:
        await asyncio.sleep(scan_interval)
        try:
            summary = await asyncio.to_thread(
                recover_expired_agent_tasks,
                lease_seconds=lease_seconds,
                recovery_claim_seconds=claim_seconds,
                batch_size=batch_size,
            )
            if summary["claimed"] or summary["errors"]:
                logger.warning("task recovery scan summary=%s", summary)
        except Exception:
            logger.exception("task recovery scan failed")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    conversation_hub.bind_loop()
    expiry_task = asyncio.create_task(_expire_pending_actions_periodically())
    heartbeat_task = asyncio.create_task(_renew_task_leases_periodically())
    recovery_task = asyncio.create_task(_recover_tasks_periodically())
    try:
        yield
    finally:
        for background_task in (expiry_task, heartbeat_task, recovery_task):
            background_task.cancel()
        for background_task in (expiry_task, heartbeat_task, recovery_task):
            with suppress(asyncio.CancelledError):
                await background_task


app = FastAPI(
    title="智能消费售后与订单服务 Agent",
    version="0.1.0",
    lifespan=_lifespan,
)
app.include_router(conversation_websocket_router)
app.add_exception_handler(pymysql.MySQLError, database_error_response)

app.add_middleware(
    RateLimitMiddleware
)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "message": "Consumer Service Agent API is running"
    }


@app.post("/auth/login")
def login(request: LoginRequest):
    """仅用于开发/测试身份链路；生产环境不允许凭 user_id 自助签发 Token。"""
    if get_settings().app_env == "production":
        raise HTTPException(status_code=403, detail="模拟登录在生产环境不可用")
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        user = get_user_by_id(cursor, request.user_id)
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"access_token": create_access_token(request.user_id), "token_type": "bearer"}


def _require_path_user(user_id: int, current_user_id: int):
    if user_id != current_user_id:
        raise HTTPException(status_code=403, detail="无权访问其他用户的数据")


def _require_after_sale_owner(after_sale_id: int, current_user_id: int):
    connection = get_connection()
    cursor = None
    try:
        cursor = connection.cursor()
        after_sale = get_after_sale_by_id(cursor, after_sale_id)
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()
    if after_sale is None or after_sale["user_id"] != current_user_id:
        raise HTTPException(status_code=404, detail="售后申请不存在")


@app.get("/users/{user_id}/orders")
def get_user_orders(user_id: int, current_user_id: int = Depends(get_current_user)):
    _require_path_user(user_id, current_user_id)
    result = get_user_orders_service(
        current_user_id
    )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="用户不存在"
        )

    return result

@app.get("/users/{user_id}/orders/{order_no}")
def get_order_detail(
    user_id: int,
    order_no: str,
    current_user_id: int = Depends(get_current_user)
):
    _require_path_user(user_id, current_user_id)
    result = get_order_detail_service(
        current_user_id,
        order_no
    )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail="订单不存在"
        )

    return result

@app.post("/after-sales")
def create_after_sale_api(
    request: CreateAfterSaleRequest,
    current_user_id: int = Depends(get_current_user)
):
    result = create_after_sale_service(
        user_id=current_user_id,
        order_no=request.order_no,
        order_item_id=request.order_item_id,
        after_sale_type=request.type,
        reason=request.reason
    )

    if result["success"] is False:
        error_code = result["code"]

        if error_code == "ORDER_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail=result["message"]
            )

        if error_code == "ORDER_ITEM_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail=result["message"]
            )

        if error_code == "AFTER_SALE_ALREADY_EXISTS":
            raise HTTPException(
                status_code=409,
                detail=result["message"]
            )

        if error_code == "ORDER_STATUS_NOT_ALLOWED":
            raise HTTPException(
                status_code=400,
                detail=result["message"]
            )

        if error_code in {"INELIGIBLE", "NEEDS_REVIEW", "PREVIEW_RECHECK_REQUIRED"}:
            raise HTTPException(status_code=422, detail=result["message"])

        if error_code in {"POLICY_UNAVAILABLE", "DATABASE_RETRYABLE"}:
            raise HTTPException(status_code=503, detail=result["message"])

        raise HTTPException(
            status_code=400,
            detail="售后申请创建失败"
        )

    return result

@app.patch("/after-sales/{after_sale_id}/status")
def update_after_sale_status_api(
    after_sale_id: int,
    request: UpdateAfterSaleStatusRequest,
    current_user_id: int = Depends(get_current_user)
):
    _require_after_sale_owner(after_sale_id, current_user_id)
    result = update_after_sale_status_service(
        after_sale_id=after_sale_id,
        new_status=request.new_status
    )

    if result["success"] is False:
        error_code = result["code"]

        if error_code == "AFTER_SALE_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail=result["message"]
            )

        if error_code == "INVALID_STATUS_TRANSITION":
            raise HTTPException(
                status_code=409,
                detail=result["message"]
            )

        if error_code == "STATUS_UPDATE_FAILED":
            raise HTTPException(
                status_code=500,
                detail=result["message"]
            )

        raise HTTPException(
            status_code=400,
            detail="售后状态更新失败"
        )

    return result

@app.get("/users/{user_id}/after-sales")
def get_user_after_sales_api(
    user_id: int,
    current_user_id: int = Depends(get_current_user)
):
    _require_path_user(user_id, current_user_id)
    result = get_user_after_sales_service(
        current_user_id
    )

    if result["success"] is False:
        error_code = result["code"]

        if error_code == "USER_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail=result["message"]
            )

        raise HTTPException(
            status_code=400,
            detail="售后列表查询失败"
        )

    return result

@app.get(
    "/users/{user_id}/after-sales/{after_sale_no}"
)
def get_after_sale_detail_api(
    user_id: int,
    after_sale_no: str,
    current_user_id: int = Depends(get_current_user)
):
    _require_path_user(user_id, current_user_id)
    result = get_after_sale_detail_service(
        current_user_id,
        after_sale_no
    )

    if result["success"] is False:
        error_code = result["code"]

        if error_code == "USER_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail=result["message"]
            )

        if error_code == "AFTER_SALE_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail=result["message"]
            )

        raise HTTPException(
            status_code=400,
            detail="售后详情查询失败"
        )

    return result

@app.get(
    "/users/{user_id}/after-sales/{after_sale_no}/history"
)
def get_after_sale_history_api(
    user_id: int,
    after_sale_no: str,
    current_user_id: int = Depends(get_current_user)
):
    _require_path_user(user_id, current_user_id)
    result = get_after_sale_history_service(
        current_user_id,
        after_sale_no
    )

    if result["success"] is False:
        error_code = result["code"]

        if error_code == "USER_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail=result["message"]
            )

        if error_code == "AFTER_SALE_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail=result["message"]
            )

        raise HTTPException(
            status_code=400,
            detail="售后状态历史查询失败"
        )

    return result

@app.get("/test-rate-limit")
def test_rate_limit():
    return {
        "message": "success"
    }


@app.post("/agent/test")
def agent_test(current_user_id: int = Depends(get_current_user)):

    return {
        "message": "agent ok"
    }


@app.get("/agent/tasks")
def agent_tasks(
    conversation_id: int | None = None,
    current_user_id: int = Depends(get_current_user),
):
    if conversation_id is not None:
        try:
            get_conversation(current_user_id, conversation_id)
        except ConversationNotFound:
            raise HTTPException(status_code=404, detail="会话不存在") from None
        tasks = list_user_tasks(current_user_id, conversation_id)
    else:
        tasks = list_user_tasks(current_user_id)
    return {"success": True, "data": {"tasks": tasks}}


@app.get("/agent/conversations")
def agent_conversations(current_user_id: int = Depends(get_current_user)):
    return {"success": True, "data": {"conversations": list_conversations(current_user_id)}}


@app.post("/agent/conversations")
def agent_create_conversation(current_user_id: int = Depends(get_current_user)):
    return {"success": True, "data": create_conversation(current_user_id)}


@app.get("/agent/conversations/{conversation_id}/messages")
def agent_conversation_messages(
    conversation_id: int, current_user_id: int = Depends(get_current_user)
):
    try:
        messages = get_conversation_messages(current_user_id, conversation_id)
    except ConversationNotFound:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return {"success": True, "data": {"messages": messages}}


class HandoffRequest(BaseModel):
    reason: str = Field(default="用户请求人工客服", min_length=1, max_length=1000)


@app.post("/agent/conversations/{conversation_id}/handoff")
def agent_handoff(
    conversation_id: int,
    request: HandoffRequest,
    current_user_id: int = Depends(get_current_user),
):
    try:
        handoff = create_handoff(current_user_id, conversation_id, request.reason)
    except ConversationNotFound:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return {"success": True, "data": handoff}


@app.get("/admin/handoff/tasks", dependencies=[Depends(require_admin_key)])
def admin_handoff_tasks(status: str = "waiting"):
    try:
        tasks = list_handoff_tasks(status)
    except ValueError:
        raise HTTPException(status_code=400, detail="不支持的人工请求状态") from None
    return {"success": True, "data": {"tasks": tasks}}


@app.get("/admin/handoff/{handoff_id}", dependencies=[Depends(require_admin_key)])
def admin_handoff_detail(handoff_id: int):
    try:
        return {"success": True, "data": get_handoff_detail(handoff_id)}
    except HandoffNotFound:
        raise HTTPException(status_code=404, detail="人工请求不存在") from None


def _transition_handoff_response(handoff_id: int, old: str, new: str):
    try:
        handoff = transition_handoff(handoff_id, old, new)
        conversation_hub.publish_handoff_status(handoff["conversation_id"], new)
        if new == "completed":
            conversation_hub.revoke_admin(handoff_id, handoff["conversation_id"])
        return {"success": True, "data": handoff}
    except HandoffNotFound:
        raise HTTPException(status_code=404, detail="人工请求不存在") from None
    except HandoffStateConflict:
        raise HTTPException(status_code=409, detail="人工请求状态已变化") from None


@app.post("/admin/handoff/{handoff_id}/accept", dependencies=[Depends(require_admin_key)])
def admin_handoff_accept(handoff_id: int):
    return _transition_handoff_response(handoff_id, "waiting", "assigned")


@app.post("/admin/handoff/{handoff_id}/complete", dependencies=[Depends(require_admin_key)])
def admin_handoff_complete(handoff_id: int):
    return _transition_handoff_response(handoff_id, "assigned", "completed")


class HandoffMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


@app.post("/admin/handoff/{handoff_id}/message", dependencies=[Depends(require_admin_key)])
def admin_handoff_message(handoff_id: int, request: HandoffMessageRequest):
    try:
        message = send_handoff_message(handoff_id, request.content)
        conversation_hub.publish(message)
        return {"success": True, "data": message}
    except HandoffNotFound:
        raise HTTPException(status_code=404, detail="人工请求不存在") from None
    except HandoffStateConflict:
        raise HTTPException(status_code=409, detail="只有已接入的人工请求可以发送消息") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


POLICY_CATEGORIES = {
    "return_policy",
    "exchange_policy",
    "warranty_policy",
    "product_policy",
    "general_policy",
}
POLICY_PRODUCT_SCOPES = {"general", "phone", "earphone", "tablet", "computer"}


def _policy_document_response(row: dict) -> dict:
    """Normalize legacy Policy RAG document keys at the backend boundary."""
    return {
        "id": row.get("ID"),
        "file_name": row.get("文件名"),
        "file_hash": row.get("文件hash"),
        "file_type": row.get("文件类型"),
        "category": row.get("分类") or "general_policy",
        "product_scope": row.get("商品范围") or "general",
        "version": row.get("版本") or "v1",
        "created_by": row.get("创建人") or "legacy",
        "chunk_count": row.get("文本块数量") or 0,
        "created_at": row.get("上传时间"),
        "updated_at": row.get("更新时间"),
    }


@app.get("/admin/policy/documents", dependencies=[Depends(require_admin_key)])
def admin_policy_documents():
    try:
        documents = list_policy_documents()
    except PolicyRAGAdminError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    return {"success": True, "data": {"documents": [_policy_document_response(row) for row in documents]}}


@app.post("/admin/policy/documents", dependencies=[Depends(require_admin_key)])
async def admin_policy_upload(
    file: UploadFile = File(...),
    category: str = Form(...),
    product_scope: str = Form(...),
    version: str = Form(...),
):
    category = category.strip()
    product_scope = product_scope.strip()
    version = version.strip()
    if category not in POLICY_CATEGORIES:
        raise HTTPException(status_code=422, detail="不支持的政策分类")
    if product_scope not in POLICY_PRODUCT_SCOPES:
        raise HTTPException(status_code=422, detail="不支持的商品适用范围")
    if not version or len(version) > 64:
        raise HTTPException(status_code=422, detail="政策版本不能为空且不得超过 64 个字符")

    try:
        result = await asyncio.to_thread(
            upload_policy_document,
            filename=file.filename or "unnamed",
            content=await file.read(),
            content_type=file.content_type,
            category=category,
            product_scope=product_scope,
            version=version,
            created_by="admin_workbench",
        )
    except PolicyRAGAdminError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    return {"success": True, "data": result}


@app.delete("/admin/policy/documents/{document_id}", dependencies=[Depends(require_admin_key)])
async def admin_policy_delete(document_id: int):
    try:
        result = await asyncio.to_thread(delete_policy_document, document_id)
    except PolicyRAGAdminError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    return {"success": True, "data": result}


def _is_handoff_request(message: str) -> bool:
    compact = re.sub(r"\s+", "", message)
    return bool(re.fullmatch(
        r"(?:请|帮我|我要|我想|麻烦|我需要)?(?:转人工|联系客服|人工处理|转接人工客服)(?:吧|谢谢|。|！|!|？|\?)?",
        compact,
    ))


@app.get("/agent/trace/{task_id}")
def agent_trace(
    task_id: int,
    conversation_id: int | None = None,
    current_user_id: int = Depends(get_current_user),
):
    if conversation_id is not None:
        trace = get_user_task_trace(current_user_id, task_id, conversation_id)
    else:
        trace = get_user_task_trace(current_user_id, task_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "data": trace}

import uuid

from schemas.agent_schema import (
    AgentChatRequest,
    AgentResponse
)


@app.post("/agent/chat")
def agent_chat(
    request: AgentChatRequest,
    current_user_id: int = Depends(get_current_user)
):

    trace_id = str(uuid.uuid4())
    try:
        if request.conversation_id is None:
            conversations = list_conversations(current_user_id)
            conversation_id = (
                conversations[0]["id"] if conversations else create_conversation(current_user_id)["id"]
            )
        else:
            get_conversation(current_user_id, request.conversation_id)
            conversation_id = request.conversation_id
    except ConversationNotFound:
        raise HTTPException(status_code=404, detail="会话不存在") from None

    conversation_hub.publish(save_message(current_user_id, conversation_id, "user", request.message))
    logger.info("agent/chat started user_id=%s", current_user_id, extra=log_extra(trace_id=trace_id))

    try:
        with conversation_scope(current_user_id, conversation_id):
            operation = detect_after_sale_operation(request.message)
            if _is_handoff_request(request.message):
                handoff = create_handoff(current_user_id, conversation_id, request.message)
                result = {
                    "success": True,
                    "code": "HANDOFF_REQUESTED",
                    "message": "已为您转接人工客服，请稍候",
                    "data": {"status": handoff["status"]},
                }
            elif operation is not None:
                result = prepare_after_sale_operation(request.message, current_user_id, trace_id, operation)
            else:
                if get_settings().use_langgraph_agent:
                    from agent.graph.runtime import run_graph_agent
                    agent_runner = run_graph_agent
                else:
                    agent_runner = run_agent
                result = agent_runner(
                    user_text=request.message,
                    current_user_id=current_user_id,
                    trace_id=trace_id
                )
    except pymysql.MySQLError:
        # Do not attempt another database write or claim a persisted failure.
        logger.error("agent/chat database state requires verification", extra=log_extra(trace_id=trace_id))
        raise
    except Exception:
        logger.exception("agent/chat raised", extra=log_extra(trace_id=trace_id))
        try:
            conversation_hub.publish(save_message(
                current_user_id, conversation_id, "assistant", "服务暂时不可用，请稍后重试。"
            ))
        except Exception:
            logger.exception("agent/chat failed to save error response", extra=log_extra(trace_id=trace_id))
        raise
    logger.info(
        "agent/chat finished code=%s",
        result.get("code", "OK") if isinstance(result, dict) else "OK",
        extra=log_extra(trace_id=trace_id),
    )


    # workflow 已经返回业务结果
    if isinstance(result, dict):

        response = AgentResponse(
            success=result.get(
                "success",
                True
            ),
            code=result.get(
                "code",
                "OK"
            ),
            message=result.get(
                "message",
                "处理成功"
            ),
            data=result.get(
                "data",
                None
            ),
            trace_id=trace_id
        )
        response_data = response.data if isinstance(response.data, dict) else {}
        answer = response_data.get("answer") or response.message
        if response.code == "ACTION_PREVIEW_READY" and isinstance(response_data.get("preview"), dict):
            preview = response_data["preview"]
            if preview.get("operation") == "cancel_after_sale":
                answer = f"请确认取消售后：{preview.get('product_name', '')}，申请编号 {preview.get('after_sale_no', '')}。"
            elif preview.get("operation") == "change_after_sale_type":
                answer = f"请确认修改售后类型：{preview.get('product_name', '')}，申请编号 {preview.get('after_sale_no', '')}。"
            else:
                answer = (
                    f"请确认售后申请：商品 {preview.get('product_name', '')}，"
                    f"类型 {preview.get('after_sale_type', '')}，原因 {preview.get('reason', '')}。"
                )
        conversation_hub.publish(save_message(current_user_id, conversation_id, "assistant", str(answer)))
        return response


    # 普通文本结果
    response = AgentResponse(
        success=True,
        code="OK",
        message=result,
        data=None,
        trace_id=trace_id
    )
    conversation_hub.publish(save_message(current_user_id, conversation_id, "assistant", response.message))
    return response

@app.post("/agent/cancel")
def agent_cancel(
    request: AgentConfirmRequest,
    current_user_id: int = Depends(get_current_user),
):
    if request.conversation_id is not None and get_pending_action(
        request.confirmation_id, current_user_id, request.conversation_id
    ) is None:
        return {"success": False, "code": "ACTION_NOT_FOUND", "message": "待确认操作不存在"}
    return cancel_action(request.confirmation_id, current_user_id)


@app.post("/agent/confirm")
def agent_confirm(
    request: AgentConfirmRequest,
    current_user_id: int = Depends(get_current_user)
):

    trace_id = str(uuid.uuid4())

    # =====================================================
    # 1. 使用请求中的资源 ID 确认；所有权由 Confirmation Service 校验
    # =====================================================

    confirmation_id = request.confirmation_id
    if request.conversation_id is None:
        action = get_pending_action(confirmation_id, current_user_id)
    else:
        action = get_pending_action(confirmation_id, current_user_id, request.conversation_id)
        if action is None:
            return {"success": False, "code": "ACTION_NOT_FOUND", "message": "待确认操作不存在", "trace_id": trace_id}
    task_id = action.get("task_id") if action else None
    log_fields = log_extra(trace_id=trace_id, task_id=task_id, confirmation_id=confirmation_id)
    logger.info("agent/confirm started user_id=%s", current_user_id, extra=log_fields)

    # =====================================================
    # 2. pending -> confirmed
    # =====================================================

    with trace_step(task_id, trace_id, "confirm", {
        "confirmation_id": confirmation_id, "user_id": current_user_id
    }) as step:
        confirm_result = confirm_action(
            confirmation_id=confirmation_id,
            current_user_id=current_user_id,
            trace_id=trace_id
        )
        step.set_result(confirm_result)

    if confirm_result["code"] == "IDEMPOTENT_REPLAY":
        logger.info("agent/confirm replay", extra=log_fields)
        return {
            **confirm_result,
            "trace_id": trace_id
        }

    # A prior request may have confirmed but lost its response before claiming.
    # Re-enter the executor with the same resource; its CAS permits one winner.
    if confirm_result["success"] is False and confirm_result["code"] != "ACTION_ALREADY_CONFIRMED":
        logger.warning("agent/confirm rejected code=%s", confirm_result["code"], extra=log_fields)

        return {

            "success": False,

            "code":
                confirm_result["code"],

            "message":
                confirm_result["message"],

            "trace_id":
                trace_id

        }

    # =====================================================
    # 3. confirmed -> executing -> executed
    # =====================================================

    with trace_step(task_id, trace_id, "execute", {
        "confirmation_id": confirmation_id, "user_id": current_user_id
    }) as step:
        execute_result = execute_confirmed_action(
            confirmation_id=confirmation_id,
            current_user_id=current_user_id,
            trace_id=trace_id
        )
        step.set_result(execute_result)

    logger.info(
        "agent/confirm finished code=%s", execute_result["code"], extra=log_fields
    )

    return {

        "success":

            execute_result["success"],

        "code":

            execute_result["code"],

        "message":

            execute_result["message"],

        "data":

            execute_result.get(
                "data"
            ),

        "trace_id":

            trace_id

    }


@app.get("/ready")
def ready():
    """依赖就绪检查；/health 保留为进程存活检查。"""
    try:
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        finally:
            connection.close()
        if not check_redis():
            raise RuntimeError("Redis ping failed")
    except Exception:
        logger.exception("readiness check failed")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}
