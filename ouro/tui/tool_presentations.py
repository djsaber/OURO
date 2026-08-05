from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from ouro.application.supervisor_models import RuntimeToolCallActivity

ToolLineState = Literal["pending", "active", "done", "muted"]

_COMMAND_TOOLS = frozenset({"bash", "commandprompt", "powershell", "sh"})
_MAX_COMMAND_LENGTH = 120
_MAX_PATH_LENGTH = 80
_MAX_TODO_LINES = 12


@dataclass(frozen=True)
class ToolPresentationLine:
    marker: str
    text: str
    state: ToolLineState = "muted"


@dataclass(frozen=True)
class ToolPresentation:
    subject: str | None = None
    summary: str | None = None
    lines: tuple[ToolPresentationLine, ...] = ()


def present_tool(tool: RuntimeToolCallActivity) -> ToolPresentation:
    """Project canonical tool activity into compact, tool-aware display content."""

    name = tool.tool_name.casefold()
    if name == "read":
        return _present_read(tool.arguments, tool.result_data)
    if name == "write":
        return _present_write(tool.arguments)
    if name == "edit":
        return _present_edit(tool.arguments)
    if name in _COMMAND_TOOLS:
        return _present_command(tool.arguments)
    if name == "todolist":
        return _present_todos(tool.arguments, tool.result_data)
    return ToolPresentation()


def _present_read(
    arguments: dict[str, JsonValue],
    result: dict[str, JsonValue],
) -> ToolPresentation:
    path = _path(arguments.get("path"))
    line_count = _non_negative_int(result.get("line_count"))
    summary: str | None
    if line_count is not None:
        summary = _plural(line_count, "line")
    else:
        limit = _positive_int(arguments.get("limit"))
        summary = f"up to {_plural(limit, 'line')}" if limit is not None else None
    return ToolPresentation(subject=path, summary=summary)


def _present_write(arguments: dict[str, JsonValue]) -> ToolPresentation:
    path = _path(arguments.get("path"))
    content = arguments.get("content")
    line_count = _text_line_count(content) if isinstance(content, str) else None
    mode = arguments.get("mode", "overwrite")
    parts = []
    if line_count is not None:
        parts.append(_plural(line_count, "line"))
    if mode in {"overwrite", "append"}:
        parts.append(str(mode))
    return ToolPresentation(subject=path, summary=" · ".join(parts) or None)


def _present_edit(arguments: dict[str, JsonValue]) -> ToolPresentation:
    path = _path(arguments.get("path"))
    old = arguments.get("old")
    new = arguments.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return ToolPresentation(subject=path)
    summary = f"+{_text_line_count(new)} −{_text_line_count(old)} lines"
    if arguments.get("replace_all") is True:
        summary += " · replace all"
    return ToolPresentation(subject=path, summary=summary)


def _present_command(arguments: dict[str, JsonValue]) -> ToolPresentation:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolPresentation()
    flattened = " ↵ ".join(part.strip() for part in command.splitlines() if part.strip())
    return ToolPresentation(
        lines=(ToolPresentationLine("└", _truncate_end(flattened, _MAX_COMMAND_LENGTH)),)
    )


def _present_todos(
    arguments: dict[str, JsonValue],
    result: dict[str, JsonValue],
) -> ToolPresentation:
    raw_todos = arguments.get("todos")
    if not isinstance(raw_todos, list):
        raw_todos = result.get("todos")
    if not isinstance(raw_todos, list):
        return ToolPresentation(summary="read current plan")

    lines: list[ToolPresentationLine] = []
    done_count = 0
    valid_count = 0
    for raw in raw_todos:
        if not isinstance(raw, dict):
            continue
        title = raw.get("title")
        status = raw.get("status")
        if not isinstance(title, str) or status not in {"pending", "in_progress", "done"}:
            continue
        valid_count += 1
        marker: str
        state: ToolLineState
        if status == "done":
            done_count += 1
            marker, state = "✓", "done"
        elif status == "in_progress":
            marker, state = "◉", "active"
        else:
            marker, state = "○", "pending"
        if len(lines) < _MAX_TODO_LINES:
            lines.append(ToolPresentationLine(marker, title, state))

    hidden_count = valid_count - len(lines)
    if hidden_count:
        lines.append(ToolPresentationLine("…", f"{hidden_count} more item(s)"))
    summary = f"{done_count}/{valid_count} done" if valid_count else "empty"
    return ToolPresentation(summary=summary, lines=tuple(lines))


def _path(value: JsonValue | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _truncate_middle(value, _MAX_PATH_LENGTH)


def _text_line_count(value: str) -> int:
    return len(value.splitlines()) if value else 0


def _non_negative_int(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _positive_int(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun if count == 1 else noun + 's'}"


def _truncate_end(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _truncate_middle(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    left = (limit - 1) // 2
    right = limit - left - 1
    return value[:left] + "…" + value[-right:]


__all__ = ["ToolPresentation", "ToolPresentationLine", "present_tool"]
