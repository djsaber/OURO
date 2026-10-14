"""Stateless validation and rendering of a supplied todo list."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin_common import BuiltinArguments, _tool_result

TODOLIST_DESCRIPTION = """\
Track multi-step work in a todo list for this conversation. Skip trivial tasks and purely
informational replies. In plan mode, use the plan file instead.
Submit the complete current list on every call; [] represents an empty list. The tool validates
and displays the supplied items without storing state. Refer to conversation history for prior
lists. Capture new multi-step requirements as todos.
Update only after meaningful changes. Mark one item in_progress before starting it, keep exactly
one active while working, and mark it done immediately after completion. Do not mark
incomplete, unverified, failing, or blocked work done. Keep blocked work in_progress or add a
pending item for the blocker; tell the user if no further progress is possible."""


_TODOLIST_GUIDANCE = (
    "Ensure that you continue to use the todo list to track progress. Mark tasks done "
    "immediately after finishing them, and keep exactly one task in_progress when work is "
    "underway."
)


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
        description="Complete current todo list to display; [] represents an empty list.",
    )


def builtin_todolist_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name="TodoList",
            description=TODOLIST_DESCRIPTION,
            parameters_schema=_TodoListArguments.model_json_schema(),
            permission_mode="auto",
            effect="read_only",
        ),
        handler=_todolist,
    )


def _todolist(
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult:
    arguments = _TodoListArguments.model_validate(invocation.arguments)
    todos = arguments.todos
    content = f"{_render_todos(todos)}\n\n{_TODOLIST_GUIDANCE}" if todos else "Todo list is empty."
    return _tool_result(
        invocation,
        content=content,
        structured={"todos": [item.to_json() for item in todos]},
    )


def _render_todos(todos: list[TodoItem]) -> str:
    lines = "\n".join(f"  [{item.status}] {item.title}" for item in todos)
    return f"Current todo list:\n{lines}"


__all__ = [
    "TODOLIST_DESCRIPTION",
    "TodoItem",
    "TodoStatus",
    "builtin_todolist_tool",
]
