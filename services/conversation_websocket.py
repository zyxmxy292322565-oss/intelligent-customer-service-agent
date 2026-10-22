"""Authenticated live transport for persisted conversation messages."""

import asyncio
import hmac

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from auth.jwt import decode_access_token
from core.settings import get_settings
from services.conversation_realtime import conversation_hub
from services.conversation_service import (
    ConversationNotFound, get_conversation, get_conversation_messages_since, save_message,
)
from services.handoff_admin_service import (
    HandoffStateConflict, get_assigned_handoff_for_conversation, send_handoff_message,
)


router = APIRouter()


@router.websocket("/ws/conversations/{conversation_id}")
async def conversation_websocket(websocket: WebSocket, conversation_id: int):
    bearer = websocket.headers.get("authorization")
    admin_key = websocket.headers.get("x-admin-key")
    if conversation_id <= 0 or bool(bearer) == bool(admin_key):
        await websocket.close(code=4401)
        return

    user_id = None
    handoff_id = None
    if bearer:
        scheme, _, token = bearer.partition(" ")
        if scheme.lower() != "bearer" or not token:
            await websocket.close(code=4401)
            return
        try:
            user_id = decode_access_token(token)
            await asyncio.to_thread(get_conversation, user_id, conversation_id)
        except jwt.InvalidTokenError:
            await websocket.close(code=4401)
            return
        except ConversationNotFound:
            await websocket.close(code=4403)
            return
        role = "user"
    else:
        configured = get_settings().admin_api_key
        if not configured or not hmac.compare_digest(admin_key, configured):
            await websocket.close(code=4403)
            return
        handoff = await asyncio.to_thread(get_assigned_handoff_for_conversation, conversation_id)
        if handoff is None:
            await websocket.close(code=4403)
            return
        handoff_id = handoff["id"]
        role = "admin"

    await websocket.accept()
    subscriber = conversation_hub.attach(websocket, conversation_id, role, handoff_id)
    try:
        assigned = handoff_id is not None or await asyncio.to_thread(
            get_assigned_handoff_for_conversation, conversation_id
        ) is not None
        await subscriber.send({
            "type": "ready", "conversation_id": conversation_id,
            "connection_id": subscriber.connection_id, "role": role,
            "handoff_assigned": assigned,
        })
        while True:
            try:
                event = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            except asyncio.TimeoutError:
                # A long-lived customer socket must not outlive its JWT indefinitely.
                if bearer:
                    try:
                        decode_access_token(token)
                    except jwt.InvalidTokenError:
                        await websocket.close(code=4401)
                        break
                else:
                    current_handoff = await asyncio.to_thread(
                        get_assigned_handoff_for_conversation, conversation_id
                    )
                    if current_handoff is None or current_handoff["id"] != handoff_id:
                        await websocket.close(code=4403)
                        break
                continue
            if not isinstance(event, dict):
                await subscriber.send({"type": "error", "code": "INVALID_EVENT"})
                continue
            if bearer:
                try:
                    decode_access_token(token)
                except jwt.InvalidTokenError:
                    await websocket.close(code=4401)
                    break
            kind = event.get("type")
            if kind == "sync":
                after_id = event.get("after_id")
                if type(after_id) is not int or after_id < 0:
                    await subscriber.send({"type": "error", "code": "INVALID_CURSOR"})
                    continue
                if role == "admin":
                    current_handoff = await asyncio.to_thread(
                        get_assigned_handoff_for_conversation, conversation_id
                    )
                    if current_handoff is None or current_handoff["id"] != handoff_id:
                        await websocket.close(code=4403)
                        break
                rows = await asyncio.to_thread(
                    get_conversation_messages_since, conversation_id, after_id
                )
                for row in rows:
                    await subscriber.send({"type": "message", "message": row})
                await subscriber.send({"type": "sync_complete"})
            elif kind == "message":
                content = event.get("content")
                if not isinstance(content, str) or not 0 < len(content.strip()) <= 2000:
                    await subscriber.send({"type": "error", "code": "INVALID_MESSAGE"})
                    continue
                if role == "admin":
                    try:
                        saved = await asyncio.to_thread(send_handoff_message, handoff_id, content)
                    except HandoffStateConflict:
                        await websocket.close(code=4403)
                        break
                else:
                    if await asyncio.to_thread(
                        get_assigned_handoff_for_conversation, conversation_id
                    ) is None:
                        await subscriber.send({"type": "error", "code": "HANDOFF_NOT_ASSIGNED"})
                        continue
                    saved = await asyncio.to_thread(
                        save_message, user_id, conversation_id, "user", content.strip()
                    )
                await conversation_hub.broadcast(saved)
            elif kind == "ping":
                await subscriber.send({"type": "pong"})
            else:
                await subscriber.send({"type": "error", "code": "UNKNOWN_EVENT"})
    except WebSocketDisconnect:
        pass
    finally:
        conversation_hub.detach(subscriber)
