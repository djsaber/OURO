"""Built-in TodoList tool definition and session-scoped state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import JsonValue

from ouro.core.harness_calls import HarnessInvocation
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin_common import _tool_result

_TODOLIST_STATE_OWNER = "ouro.host"


TODOLIST_DESCRIPTION = """\
Use this tool to create, inspect, or update a structured TODO list for a multi-step task. Use \
it when progress tracking helps the current work. This is especially \
useful in long-running investigations and implementation tasks with several tool calls; in \
plan mode, write the plan to the plan file rather than tracking it here.

When to use:

- Multi-step tasks that span several tool calls
- Tracking investigation progress across a large codebase search
- Planning a sequence of edits before making them
- After receiving new multi-step instructions, capture the requirements as todos
- Before starting a tracked task, mark exactly one item as `in_progress`
- Immediately after finishing a tracked task, mark it `done`; do not batch completions at the end

When NOT to use:

- Single-shot answers that complete in one or two tool calls
- Trivial requests where tracking adds no clarity
- Purely conversational or informational replies

Avoid churn:

- Do not re-call this tool when nothing meaningful has changed since the last call — update the \
list only after real progress.
- When unsure of the current state, call query mode first (omit `todos`) to check the list \
before deciding what to update.
- If no available tool can move any task forward, tell the user where you are stuck instead of \
repeatedly re-ordering the same todos.

How to use:

- Call with `todos: [...]` to replace the full list. Statuses: pending / in_progress / done.
- Call with no `todos` argument to retrieve the current list without changing it.
- Call with `todos: []` to clear the list.
- Keep titles short and actionable (e.g. "Read session-control.ts", "Add planMode flag to \
TurnManager").
- Update statuses as you make progress.
- When work is underway, keep exactly one task `in_progress`.
- Only mark a task `done` when it is fully accomplished.
- Never mark a task `done` if tests are failing, implementation is partial, unresolved errors \
remain, or required files/dependencies could not be found.
- If you encounter a blocker, keep the blocked task `in_progress` or add a new pending task \
describing what must be resolved."""


_TODOLIST_GUIDANCE = (
    "Ensure that you continue to use the todo list to track progress. Mark tasks done "
    "immediately after finishing them, and keep exactly one task in_progress when work is "
    "underway."
)


_TODOLIST_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": (
                "Complete replacement for the current todo list. Omit this field to read the "
                "list without changing it; pass an empty array to clear it."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Short, imperative action describing one verifiable unit of work."
                        ),
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "done"],
                        "description": (
                            "Use `pending` before work starts, `in_progress` for the single "
                            "active item, and `done` only after the item is fully complete."
                        ),
                    },
                },
                "required": ["title", "status"],
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}


_MISSING = object()


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

    def __init__(
        self,
        state: HarnessInvocation,
        *,
        session_id: str,
        owner_id: str,
    ) -> None:
        self._state = state
        self._session_id = session_id
        self._owner_id = owner_id

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
            SessionExtensionStateRecord(
                session_id=self._session_id,
                owner_id=self._owner_id,
                state_schema_version=_STATE_SCHEMA_VERSION,
                payload=cast(JsonValue, {"todos": [item.to_json() for item in todos]}),
                branch_policy="reset",
            )
        )

    def clear(self) -> None:
        self._state.delete()


def builtin_todolist_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name="TodoList",
            description=TODOLIST_DESCRIPTION,
            parameters_schema=dict(_TODOLIST_SCHEMA),
            permission_mode="auto",
            effect="state_changing",
            batch_behavior="exclusive",
        ),
        handler=_todolist,
    )


def _todolist(
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult:
    if context.harness is None:
        raise RuntimeError("TodoList requires an active Session state")
    state = HarnessInvocation(
        context.harness,
        _TODOLIST_STATE_OWNER,
        writable=True,
        signal=context.signal,
    )
    try:
        repository = TodoListRepository(
            state,
            session_id=invocation.session_id,
            owner_id=_TODOLIST_STATE_OWNER,
        )
        raw = invocation.arguments.get("todos", _MISSING)
        if raw is _MISSING:
            todos = repository.load()
            content = _render_todos(todos) if todos else "Todo list is empty."
            return _tool_result(
                invocation,
                content=content,
                structured={"todos": [item.to_json() for item in todos]},
                harness_change=state.finish(),
            )
        if not isinstance(raw, list):
            raise ValueError("tool argument 'todos' must be a list")
        todos = tuple(TodoItem.from_json(item) for item in raw)
        if not todos:
            repository.clear()
            return _tool_result(
                invocation,
                content="Todo list cleared.",
                structured={"todos": []},
                harness_change=state.finish(),
            )
        repository.save(todos)
        return _tool_result(
            invocation,
            content=f"Todo list updated.\n{_render_todos(todos)}\n\n{_TODOLIST_GUIDANCE}",
            structured={"todos": [item.to_json() for item in todos]},
            harness_change=state.finish(),
        )
    finally:
        state.close()


def _render_todos(todos: tuple[TodoItem, ...]) -> str:
    lines = "\n".join(f"  [{item.status}] {item.title}" for item in todos)
    return f"Current todo list:\n{lines}"


__all__ = [
    "TODOLIST_DESCRIPTION",
    "TodoItem",
    "TodoListRepository",
    "TodoStatus",
    "builtin_todolist_tool",
]
