"""Built-in TodoList tool definition and session-scoped state."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import ConfigDict, Field, JsonValue

from ouro.core.harness_calls import HarnessInvocation
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin_common import BuiltinArguments, _tool_result

_TODOLIST_STATE_OWNER = "ouro.host"


TODOLIST_DESCRIPTION = """\
Track multi-step work in a todo list for this conversation. Skip trivial tasks and purely
informational replies. In plan mode, use the plan file instead.
Capture new multi-step requirements as todos; read the list first if unsure of its state.
Update only after meaningful changes. Mark one item in_progress before starting it, keep exactly
one active while working, and mark it done immediately after completion. Do not mark
incomplete, unverified, failing, or blocked work done. Keep blocked work in_progress or add a
pending item for the blocker; tell the user if no further progress is possible."""


_TODOLIST_GUIDANCE = (
    "Ensure that you continue to use the todo list to track progress. Mark tasks done "
    "immediately after finishing them, and keep exactly one task in_progress when work is "
    "underway."
)


_STATE_SCHEMA_VERSION = 1


TodoStatus = Literal["pending", "in_progress", "done"]


class TodoItem(BuiltinArguments):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, description="Short action describing one verifiable task.")
    status: TodoStatus = Field(
        description="pending: not started; in_progress: active; done: fully completed.",
    )

    def to_json(self) -> dict[str, object]:
        return {"title": self.title, "status": self.status}


class _TodoListArguments(BuiltinArguments):
    todos: list[TodoItem] = Field(
        default_factory=list,
        description="Full replacement list. Omit to read without changes; [] clears it.",
    )


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
        return tuple(TodoItem.model_validate(item) for item in todos)

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
            parameters_schema=_TodoListArguments.model_json_schema(),
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
    arguments = _TodoListArguments.model_validate(invocation.arguments)
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
        if "todos" not in arguments.model_fields_set:
            todos = repository.load()
            content = _render_todos(todos) if todos else "Todo list is empty."
            return _tool_result(
                invocation,
                content=content,
                structured={"todos": [item.to_json() for item in todos]},
                harness_change=state.finish(),
            )
        todos = tuple(arguments.todos)
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
