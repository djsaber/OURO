"""Session-scoped TODO list state for the builtin toolkit plugin."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

from ouro.sdk.plugins.v1 import JsonValue, PluginSessionStateStore

_STATE_SCHEMA_VERSION = 1
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

TodoStatus = Literal["pending", "in_progress", "done"]
_TODO_STATUSES = frozenset({"pending", "in_progress", "done"})


@dataclass(frozen=True)
class TodoItem:
    title: str
    status: TodoStatus = "pending"

    @classmethod
    def from_json(cls, raw: object) -> TodoItem:
        if not isinstance(raw, dict):
            raise ValueError("TodoList items must be objects")
        title = raw.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError("TodoList item title must be a non-empty string")
        status = raw.get("status")
        if status not in _TODO_STATUSES:
            raise ValueError("TodoList item status must be one of: pending, in_progress, done")
        return cls(title=title, status=cast(TodoStatus, status))

    def to_json(self) -> dict[str, object]:
        return {"title": self.title, "status": self.status}


class TodoListRepository:
    """Persist each Session's list independently through the plugin data store."""

    def __init__(self, state: PluginSessionStateStore) -> None:
        self._state = state

    def load(self, session_id: str) -> tuple[TodoItem, ...]:
        persisted = self._state.load(self._validated_session_id(session_id))
        if persisted is None:
            return ()
        if persisted.state_schema_version != _STATE_SCHEMA_VERSION:
            raise ValueError("unsupported TodoList state schema")
        raw = persisted.payload
        if not isinstance(raw, dict):
            raise ValueError("TodoList state must be an object")
        todos = raw.get("todos")
        if not isinstance(todos, list):
            raise ValueError("TodoList state todos must be a list")
        return tuple(TodoItem.from_json(item) for item in todos)

    def save(self, session_id: str, todos: tuple[TodoItem, ...]) -> None:
        if not todos:
            self.clear(session_id)
            return
        self._state.save(
            self._validated_session_id(session_id),
            state_schema_version=_STATE_SCHEMA_VERSION,
            payload=cast(JsonValue, {"todos": [item.to_json() for item in todos]}),
            branch_policy="reset",
        )

    def clear(self, session_id: str) -> None:
        self._state.delete(self._validated_session_id(session_id))

    @staticmethod
    def _validated_session_id(session_id: str) -> str:
        if _SESSION_ID.fullmatch(session_id) is None:
            raise ValueError("invalid TodoList Session id")
        return session_id


__all__ = ["TodoItem", "TodoListRepository", "TodoStatus"]
