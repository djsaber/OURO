from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme

ToolLineState = Literal["pending", "active", "done", "muted"]
ToolTextTone = Literal["default", "muted", "added", "removed"]

_COMMAND_TOOLS = frozenset({"bash", "commandprompt", "powershell", "sh"})
_MAX_TODO_LINES = 12


@dataclass(frozen=True)
class ToolPresentationPart:
    text: str
    tone: ToolTextTone = "default"


@dataclass(frozen=True)
class ToolPresentationLine:
    marker: str
    parts: tuple[ToolPresentationPart, ...]
    state: ToolLineState = "muted"

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts)


@dataclass(frozen=True)
class ToolPresentation:
    title: str | None = None
    summary: str | None = None
    lines: tuple[ToolPresentationLine, ...] = ()


def present_tool(
    tool: RuntimeToolCallActivity,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> ToolPresentation:
    """Project canonical tool activity into compact, tool-aware display content."""

    name = tool.tool_name.casefold()
    if name == "read":
        return _present_read(tool.arguments, tool.result_data, theme=theme)
    if name == "write":
        return _present_write(tool.arguments, theme=theme)
    if name == "edit":
        return _present_edit(tool.arguments, theme=theme)
    if name in _COMMAND_TOOLS:
        return _present_command(tool.arguments)
    if name == "todolist":
        return _present_todos(tool.arguments, tool.result_data, theme=theme)
    if name == "spawnagent":
        return _present_spawn_agent(tool.arguments, tool.result_data)
    return ToolPresentation()


def _present_spawn_agent(
    arguments: dict[str, JsonValue],
    result: dict[str, JsonValue],
) -> ToolPresentation:
    nickname = _non_blank_text(arguments.get("nickname"))
    handle = result.get("handle")
    agent_name = _non_blank_text(handle.get("agent_name")) if isinstance(handle, dict) else None
    title = nickname or agent_name or _non_blank_text(arguments.get("agent_id")) or "Subagent"
    return ToolPresentation(title=title)


def _present_read(
    arguments: dict[str, JsonValue],
    result: dict[str, JsonValue],
    *,
    theme: TuiTheme,
) -> ToolPresentation:
    path = _path(arguments.get("path"))
    offset = _positive_int(result.get("offset")) or _positive_int(arguments.get("offset")) or 1
    line_count = _non_negative_int(result.get("line_count"))
    summary: str | None
    if line_count is not None:
        summary = "0 lines" if line_count == 0 else f"{offset} - {offset + line_count - 1} lines"
    else:
        limit = _positive_int(arguments.get("limit"))
        summary = f"{offset} - {offset + limit - 1} lines" if limit is not None else None
    return ToolPresentation(lines=_file_detail_line(path, summary, theme=theme))


def _present_write(arguments: dict[str, JsonValue], *, theme: TuiTheme) -> ToolPresentation:
    path = _path(arguments.get("path"))
    content = arguments.get("content")
    line_count = _text_line_count(content) if isinstance(content, str) else None
    mode = arguments.get("mode", "overwrite")
    detail = list(_path_parts(path))
    if detail and (line_count is not None or mode in {"overwrite", "append"}):
        detail.append(ToolPresentationPart(theme.inline_separator, "muted"))
    if line_count is not None:
        detail.append(ToolPresentationPart(f"+{line_count}", "added"))
        detail.append(ToolPresentationPart(f" {_plural_noun(line_count, 'line')}", "muted"))
    if mode in {"overwrite", "append"}:
        if line_count is not None:
            detail.append(ToolPresentationPart(theme.inline_separator, "muted"))
        detail.append(ToolPresentationPart(str(mode), "muted"))
    return ToolPresentation(
        lines=(ToolPresentationLine("└", tuple(detail)),) if detail else (),
    )


def _present_edit(arguments: dict[str, JsonValue], *, theme: TuiTheme) -> ToolPresentation:
    path = _path(arguments.get("path"))
    old = arguments.get("old")
    new = arguments.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return ToolPresentation(lines=_file_detail_line(path, theme=theme))
    detail = list(_path_parts(path))
    if detail:
        detail.append(ToolPresentationPart(theme.inline_separator, "muted"))
    detail.extend(
        (
            ToolPresentationPart(f"+{_text_line_count(new)}", "added"),
            ToolPresentationPart(" ", "muted"),
            ToolPresentationPart(f"−{_text_line_count(old)}", "removed"),
            ToolPresentationPart(" lines", "muted"),
        )
    )
    if arguments.get("replace_all") is True:
        detail.append(ToolPresentationPart(f"{theme.inline_separator}replace all", "muted"))
    return ToolPresentation(lines=(ToolPresentationLine("└", tuple(detail)),))


def _present_command(arguments: dict[str, JsonValue]) -> ToolPresentation:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolPresentation()
    flattened = " ↵ ".join(part.strip() for part in command.splitlines() if part.strip())
    return ToolPresentation(
        lines=(
            ToolPresentationLine(
                "└",
                (ToolPresentationPart(flattened),),
            ),
        )
    )


def _present_todos(
    arguments: dict[str, JsonValue],
    result: dict[str, JsonValue],
    *,
    theme: TuiTheme,
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
            marker, state = theme.completed_item_marker, "done"
        elif status == "in_progress":
            marker, state = theme.active_item_marker, "active"
        else:
            marker, state = theme.pending_item_marker, "pending"
        if len(lines) < _MAX_TODO_LINES:
            lines.append(ToolPresentationLine(marker, (ToolPresentationPart(title),), state))

    hidden_count = valid_count - len(lines)
    if hidden_count:
        lines.append(
            ToolPresentationLine(
                "…",
                (ToolPresentationPart(f"{hidden_count} more item(s)"),),
            )
        )
    summary = f"{done_count}/{valid_count} done" if valid_count else "empty"
    return ToolPresentation(summary=summary, lines=tuple(lines))


def _path(value: JsonValue | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _non_blank_text(value: JsonValue | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _path_parts(path: str | None) -> tuple[ToolPresentationPart, ...]:
    return (ToolPresentationPart(path),) if path else ()


def _file_detail_line(
    path: str | None,
    summary: str | None = None,
    *,
    theme: TuiTheme,
) -> tuple[ToolPresentationLine, ...]:
    parts = list(_path_parts(path))
    if summary:
        if parts:
            parts.append(ToolPresentationPart(theme.inline_separator, "muted"))
        parts.append(ToolPresentationPart(summary, "muted"))
    return (ToolPresentationLine("└", tuple(parts)),) if parts else ()


def _text_line_count(value: str) -> int:
    return len(value.splitlines()) if value else 0


def _non_negative_int(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _positive_int(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _plural(count: int, noun: str) -> str:
    return f"{count} {_plural_noun(count, noun)}"


def _plural_noun(count: int, noun: str) -> str:
    return noun if count == 1 else noun + "s"


__all__ = [
    "ToolPresentation",
    "ToolPresentationLine",
    "ToolPresentationPart",
    "ToolTextTone",
    "present_tool",
]
