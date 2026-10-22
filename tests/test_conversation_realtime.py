"""Frontend WebSocket reconnect cursor and URL checks without network access."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))

from api.realtime import ConversationSocket, websocket_url  # noqa: E402


class _FakeConnection:
    def __init__(self, connection_id: str, message_id: int):
        self.connection_id = connection_id
        self.message_id = message_id
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def __iter__(self):
        yield json.dumps({
            "type": "ready", "connection_id": self.connection_id,
            "handoff_assigned": True,
        })
        yield json.dumps({
            "type": "message", "message": {
                "id": self.message_id, "conversation_id": 42,
                "role": "human_agent", "content": "客服回复",
            },
        })

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        pass


class RealtimeClientTests(unittest.TestCase):
    def test_url_scheme_and_reconnect_replays_from_last_id(self):
        self.assertEqual(websocket_url("http://backend:8000", 42),
                         "ws://backend:8000/ws/conversations/42")
        self.assertEqual(websocket_url("https://example.test", 42),
                         "wss://example.test/ws/conversations/42")
        first, second = _FakeConnection("one", 5), _FakeConnection("two", 6)
        with patch("api.realtime.connect", side_effect=[OSError("offline"), first, second]) as dial:
            listener = ConversationSocket("http://backend:8000", 42, token="test-token", after_id=3)
            try:
                messages = []
                while len(messages) < 2:
                    event = listener.events.get(timeout=5)
                    if event["type"] == "message":
                        messages.append(event["message"]["id"])
                self.assertEqual(messages, [5, 6])
                self.assertEqual(first.sent, [{"type": "sync", "after_id": 3}])
                self.assertEqual(second.sent, [{"type": "sync", "after_id": 5}])
                self.assertEqual(dial.call_args.kwargs["additional_headers"],
                                 {"Authorization": "Bearer test-token"})
                self.assertEqual(listener.last_id, 6)
            finally:
                listener.close()
