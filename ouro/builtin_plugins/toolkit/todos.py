"""Session-scoped TODO list state for the builtin toolkit plugin."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, cast

from ouro.sdk.plugins.v1 import PluginDataStore

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

    def __init__(self, data: PluginDataStore) -> None:
        self._data = data

    def load(self, session_id: str) -> tuple[TodoItem, ...]:
        try:
            raw = json.loads(self._data.read_bytes(self._path(session_id)).decode("utf-8"))
        except FileNotFoundError:
            return ()
        if not isinstance(raw, dict) or raw.get("schema_version") != _STATE_SCHEMA_VERSION:
            raise ValueError("unsupported TodoList state schema")
        todos = raw.get("todos")
        if not isinstance(todos, list):
            raise ValueError("TodoList state todos must be a list")
        return tuple(TodoItem.from_json(item) for item in todos)

    def save(self, session_id: str, todos: tuple[TodoItem, ...]) -> None:
        if not todos:
            self.clear(session_id)
            return
        payload = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "todos": [item.to_json() for item in todos],
        }
        self._data.write_bytes(
            self._path(session_id),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    def clear(self, session_id: str) -> None:
        self._data.delete(self._path(session_id))

    @staticmethod
    def _path(session_id: str) -> str:
        if _SESSION_ID.fullmatch(session_id) is None:
            raise ValueError("Session id cannot be used as a TodoList data path")
        return f"todos/{session_id}.json"


__all__ = ["TodoItem", "TodoListRepository", "TodoStatus"]
