"""Background WebSocket listener for a single Streamlit browser session."""

import json
import logging
import queue
import threading
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect


logger = logging.getLogger(__name__)


def websocket_url(base_url: str, conversation_id: int) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("BACKEND_URL must use http or https")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, f"/ws/conversations/{conversation_id}", "", ""))


class ConversationSocket:
    def __init__(self, base_url: str, conversation_id: int, *, token: str | None = None,
                 admin_key: str | None = None, after_id: int = 0):
        if bool(token) == bool(admin_key):
            raise ValueError("Exactly one WebSocket credential is required")
        self.conversation_id = conversation_id
        self.url = websocket_url(base_url, conversation_id)
        self.headers = ({"Authorization": f"Bearer {token}"} if token else
                        {"X-Admin-Key": admin_key})
        self.last_id = after_id
        self.connection_id: str | None = None
        self.handoff_assigned = False
        self.ready = threading.Event()
        self.events: queue.Queue[dict] = queue.Queue()
        self._stop = threading.Event()
        self._socket = None
        self._send_lock = threading.Lock()
        self._thread = threading.Thread(target=self._listen, daemon=True,
                                        name=f"conversation-ws-{conversation_id}")
        self._thread.start()

    def _listen(self) -> None:
        delay = 0.5
        while not self._stop.is_set():
            try:
                with connect(self.url, additional_headers=self.headers, proxy=None,
                             open_timeout=5, close_timeout=1) as socket:
                    self._socket = socket
                    delay = 0.5
                    for raw in socket:
                        event = json.loads(raw)
                        if event.get("type") == "ready":
                            self.connection_id = event.get("connection_id")
                            self.handoff_assigned = bool(event.get("handoff_assigned"))
                            self.ready.set()
                            with self._send_lock:
                                socket.send(json.dumps({"type": "sync", "after_id": self.last_id}))
                        elif event.get("type") == "handoff_status":
                            self.handoff_assigned = event.get("status") == "assigned"
                        elif event.get("type") == "message":
                            message_id = event.get("message", {}).get("id")
                            if isinstance(message_id, int):
                                self.last_id = max(self.last_id, message_id)
                        self.events.put(event)
            except Exception as exc:
                if not self._stop.is_set():
                    logger.warning("Conversation WebSocket disconnected: %s", exc)
            finally:
                self._socket = None
                self.ready.clear()
                self.connection_id = None
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 10)

    def send_message(self, content: str) -> bool:
        socket = self._socket
        if socket is None or not self.ready.is_set():
            return False
        try:
            with self._send_lock:
                socket.send(json.dumps({"type": "message", "content": content}, ensure_ascii=False))
            return True
        except Exception:
            logger.warning("Conversation WebSocket send failed", exc_info=True)
            return False

    def drain(self) -> list[dict]:
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result

    def close(self) -> None:
        self._stop.set()
        socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except Exception:
                logger.debug("WebSocket was already closed", exc_info=True)
