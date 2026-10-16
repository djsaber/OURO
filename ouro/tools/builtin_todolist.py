"""Stateless validation and rendering of a supplied todo list."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin_common import BuiltinArguments, _tool_result

TODOLIST_DESCRIPTION = """\
Display progress on multi-step work. Skip simple tasks and informational replies; in plan mode,
use the plan file instead. Submit the complete current list each time; [] displays an empty list.
Previous lists remain in conversation history and are not merged automatically.
Update after meaningful progress or changed requirements. Keep one item in_progress while working
and mark it done only when completed and verified. Keep blocked work unfinished, record the
blocker, and tell the user when further progress requires their input."""


_TODOLIST_GUIDANCE = (
    "Update this list as work progresses. Keep one item in_progress while working; "
    "mark done only after completion and verification."
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
