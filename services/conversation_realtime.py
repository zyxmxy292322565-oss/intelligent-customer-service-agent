"""In-process WebSocket fan-out; MySQL remains the message source of truth."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder


logger = logging.getLogger(__name__)


@dataclass
class Subscriber:
    connection_id: str
    conversation_id: int
    role: str
    websocket: WebSocket
    handoff_id: int | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, event: dict) -> None:
        async with self.send_lock:
            await self.websocket.send_json(jsonable_encoder(event))


class ConversationHub:
    def __init__(self):
        self._rooms: dict[int, dict[str, Subscriber]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self) -> None:
        self._loop = asyncio.get_running_loop()

    def attach(self, websocket: WebSocket, conversation_id: int, role: str,
               handoff_id: int | None = None) -> Subscriber:
        self.bind_loop()
        subscriber = Subscriber(uuid.uuid4().hex, conversation_id, role, websocket, handoff_id)
        self._rooms.setdefault(conversation_id, {})[subscriber.connection_id] = subscriber
        return subscriber

    def detach(self, subscriber: Subscriber) -> None:
        room = self._rooms.get(subscriber.conversation_id)
        if room is not None:
            room.pop(subscriber.connection_id, None)
            if not room:
                self._rooms.pop(subscriber.conversation_id, None)

    async def broadcast(self, message: dict) -> None:
        await self.broadcast_event(message["conversation_id"], {"type": "message", "message": message})

    async def broadcast_event(self, conversation_id: int, event: dict) -> None:
        for subscriber in list(self._rooms.get(conversation_id, {}).values()):
            try:
                await subscriber.send(event)
            except Exception:
                logger.warning("conversation websocket delivery failed", exc_info=True)
                self.detach(subscriber)

    async def close_admin(self, handoff_id: int, conversation_id: int) -> None:
        for subscriber in list(self._rooms.get(conversation_id, {}).values()):
            if subscriber.role == "admin" and subscriber.handoff_id == handoff_id:
                self.detach(subscriber)
                try:
                    await subscriber.websocket.close(code=4403)
                except Exception:
                    logger.debug("closed admin websocket already gone", exc_info=True)

    def publish(self, message: dict | None) -> None:
        if message is None or self._loop is None or self._loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(self.broadcast(message), self._loop)
        future.add_done_callback(self._log_delivery_failure)

    def revoke_admin(self, handoff_id: int, conversation_id: int) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(
            self.close_admin(handoff_id, conversation_id), self._loop
        )
        future.add_done_callback(self._log_delivery_failure)

    def publish_handoff_status(self, conversation_id: int, status: str) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(
            self.broadcast_event(conversation_id, {"type": "handoff_status", "status": status}),
            self._loop,
        )
        future.add_done_callback(self._log_delivery_failure)

    @staticmethod
    def _log_delivery_failure(future) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("conversation websocket dispatch failed")


conversation_hub = ConversationHub()
