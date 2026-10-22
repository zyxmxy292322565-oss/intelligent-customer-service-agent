"""Request-local identity for Agent persistence, without changing node contracts."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConversationScope:
    user_id: int
    conversation_id: int
    task_id: int | None = None
    execution_id: str | None = None


_scope: ContextVar[AgentConversationScope | None] = ContextVar(
    "agent_conversation_scope", default=None
)


def current_scope() -> AgentConversationScope | None:
    return _scope.get()


@contextmanager
def conversation_scope(user_id: int, conversation_id: int):
    token = _scope.set(AgentConversationScope(user_id, conversation_id))
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def task_scope(task_id: int, execution_id: str | None = None):
    current = _scope.get()
    if current is None:
        yield
        return
    token = _scope.set(AgentConversationScope(
        current.user_id, current.conversation_id, task_id, execution_id,
    ))
    try:
        yield
    finally:
        _scope.reset(token)
