"""Session-scoped TODO list state for the builtin toolkit plugin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from ouro.sdk.plugins.v2 import JsonValue, PluginSessionStateView

_STATE_SCHEMA_VERSION = 1

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
    """Read and stage the calling Session's TODO list through its owner view."""

    def __init__(self, state: PluginSessionStateView | None) -> None:
        if state is None:
            raise RuntimeError("Session state requires an active plugin invocation")
        self._state = state

    def load(self) -> tuple[TodoItem, ...]:
        persisted = self._state.read()
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

    def save(self, todos: tuple[TodoItem, ...]) -> None:
        if not todos:
            self.clear()
            return
        self._state.replace(
            state_schema_version=_STATE_SCHEMA_VERSION,
            payload=cast(JsonValue, {"todos": [item.to_json() for item in todos]}),
            branch_policy="reset",
        )

    def clear(self) -> None:
        self._state.delete()


__all__ = ["TodoItem", "TodoListRepository", "TodoStatus"]
