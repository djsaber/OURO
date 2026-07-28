from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from ouro.core.base import utc_now
from ouro.tools.base import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolResult,
)

_MAX_READ_LINES = 1_000
_MAX_READ_BYTES = 100_000
_LINE_READ_CHARS = _MAX_READ_BYTES + 1
_DISCARD_CHUNK_CHARS = 64 * 1_024

WriteMode = Literal["overwrite", "append"]


@dataclass(frozen=True)
class _TextPage:
    content: str
    line_count: int
    next_offset: int | None
    line_truncated: bool

    @property
    def truncated(self) -> bool:
        return self.next_offset is not None or self.line_truncated


@dataclass(frozen=True)
class _TextLine:
    content: str
    truncated: bool


def builtin_file_tools() -> list[Tool]:
    return [
        Tool(
            definition=ToolDefinition(
                name="Read",
                description=(
                    "Use this tool to inspect a known UTF-8 text file. Call it when you know "
                    "the file path and need its contents or a specific line range. Prefer Glob "
                    "to discover paths and Grep to locate text across files; do not use Read "
                    "for directory listing, cross-file search, binary files, or non-UTF-8 "
                    "content. Results use '<line number>\\t<content>' format. Use offset and "
                    "limit to page through large files, continuing from next_offset when the "
                    "result is truncated. Directories are rejected. Each call returns at most "
                    "1,000 lines or 100,000 UTF-8 bytes."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Absolute path or workspace-relative path of the UTF-8 text "
                                "file to inspect. Pass a file, not a directory."
                            ),
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 1,
                            "default": 1,
                            "description": (
                                "1-based line number to read from. Omit to start at line 1; use "
                                "a prior result's next_offset to continue paging."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _MAX_READ_LINES,
                            "default": _MAX_READ_LINES,
                            "description": (
                                "Maximum number of lines to return, from 1 to 1,000. Omit for "
                                "1,000. The 100,000-byte cap may end the page earlier."
                            ),
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                permission_mode="auto",
                effect="read_only",
            ),
            handler=_read,
        ),
        Tool(
            definition=ToolDefinition(
                name="Write",
                description=(
                    "Use this tool to create a UTF-8 text file, replace a file's complete "
                    "contents, or append text at its end. Use overwrite only when you intend to "
                    "supply the entire final file; use append only when preserving all existing "
                    "content is required. Prefer Edit for focused changes to an existing file. "
                    "Do not overwrite a file whose current contents matter until you have read "
                    "or otherwise verified it. Missing parent directories are created "
                    "automatically, and no extra text or newline is added."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Absolute or workspace-relative destination file path. Missing "
                                "parent directories are created automatically."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Exact UTF-8 text to write. Include every intended newline; the "
                                "tool adds nothing implicitly."
                            ),
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["overwrite", "append"],
                            "default": "overwrite",
                            "description": (
                                "Use 'overwrite' to replace the complete file, or 'append' to "
                                "add content at the current end. Both modes create a missing "
                                "file. Omit for 'overwrite'."
                            ),
                        },
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                permission_mode="approval",
                effect="state_changing",
            ),
            handler=_write,
        ),
        Tool(
            definition=ToolDefinition(
                name="Edit",
                description=(
                    "Use this tool to make a focused, deterministic change in a known UTF-8 "
                    "text file by replacing exact literal text. Read the relevant section first "
                    "when you need the current text or enough surrounding context to identify "
                    "the intended occurrence. Prefer Write only for new files or deliberate "
                    "full-file rewrites. Do not set replace_all unless every occurrence should "
                    "change. By default only the first occurrence is replaced; if old is not "
                    "found, the file is left unchanged. Matching normalizes line endings to LF, "
                    "while a CRLF file retains CRLF when written back."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Absolute or workspace-relative path of the existing UTF-8 text "
                                "file to modify."
                            ),
                        },
                        "old": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Exact literal text to replace; this is not a regular expression. "
                                "Include enough surrounding text to target the intended first "
                                "occurrence when the fragment may repeat."
                            ),
                        },
                        "new": {
                            "type": "string",
                            "description": (
                                "Exact replacement text. Use an empty string to delete the "
                                "matched text."
                            ),
                        },
                        "replace_all": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Set true only when every occurrence of old must be replaced. "
                                "Omit or set false to replace the first occurrence only."
                            ),
                        },
                    },
                    "required": ["path", "old", "new"],
                    "additionalProperties": False,
                },
                permission_mode="approval",
                effect="state_changing",
            ),
            handler=_edit,
        ),
    ]


def _read(
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext,
) -> ToolResult:
    path = _path_arg(invocation, context)
    if path.is_dir():
        raise IsADirectoryError(f"path is a directory, not a text file: {path}")
    offset = _integer_arg(invocation, "offset", default=1)
    limit = _integer_arg(invocation, "limit", default=_MAX_READ_LINES)
    with path.open("r", encoding="utf-8", newline=None) as stream:
        page = _read_text_page(
            stream,
            offset=offset,
            limit=limit,
            max_output_chars=context.max_output_chars,
        )
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=page.content,
        structured={
            "kind": "file",
            "path": str(path),
            "offset": offset,
            "limit": limit,
            "line_count": page.line_count,
            "output_bytes": len(page.content.encode("utf-8")),
            "next_offset": page.next_offset,
            "line_truncated": page.line_truncated,
            "truncated": page.truncated,
        },
        ended_at=utc_now(),
    )


def _write(
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext,
) -> ToolResult:
    path = _path_arg(invocation, context)
    content = _text_arg(invocation, "content")
    mode = _write_mode_arg(invocation)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w" if mode == "overwrite" else "a", encoding="utf-8", newline="") as file:
        file.write(content)
    byte_count = len(content.encode("utf-8"))
    action = "Wrote" if mode == "overwrite" else "Appended"
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=f"{action} {byte_count} UTF-8 bytes to {path}",
        structured={"path": str(path), "bytes": byte_count, "mode": mode},
        ended_at=utc_now(),
    )


def _edit(
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext,
) -> ToolResult:
    path = _path_arg(invocation, context)
    old = _to_lf(_text_arg(invocation, "old"))
    new = _to_lf(_text_arg(invocation, "new"))
    replace_all = _boolean_arg(invocation, "replace_all", default=False)
    with path.open("r", encoding="utf-8", newline="") as file:
        raw = file.read()
    # Match against the same universal-newline view the Read tool presents, so
    # text copied from Read output also matches files with CRLF line endings.
    text = _to_lf(raw)
    occurrence_count = text.count(old)
    if occurrence_count == 0:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            is_error=True,
            content="Old text was not found.",
            structured={"path": str(path), "changed": False, "replacements": 0},
            ended_at=utc_now(),
        )
    replacements = occurrence_count if replace_all else 1
    updated = text.replace(old, new, -1 if replace_all else 1)
    if updated != text:
        if _is_pure_crlf(raw):
            # Keep the file's original CRLF line endings on write back.
            updated = updated.replace("\n", "\r\n")
        with path.open("w", encoding="utf-8", newline="") as file:
            file.write(updated)
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=f"Edited {path}; replaced {replacements} occurrence(s)",
        structured={
            "path": str(path),
            "changed": updated != text,
            "replacements": replacements,
            "replace_all": replace_all,
        },
        ended_at=utc_now(),
    )


def _read_text_page(
    stream: TextIO,
    *,
    offset: int,
    limit: int,
    max_output_chars: int | None,
) -> _TextPage:
    if not _skip_lines(stream, offset - 1):
        return _TextPage(content="", line_count=0, next_offset=None, line_truncated=False)

    rendered_lines: list[str] = []
    used_bytes = 0
    used_chars = 0
    current_line_number = offset
    next_offset: int | None = None
    line_truncated = False

    while len(rendered_lines) < limit:
        line_start = stream.tell()
        line = _read_bounded_line(stream)
        if line is None:
            break
        separator = "\n" if rendered_lines else ""
        prefix = f"{current_line_number}\t"
        candidate = f"{separator}{prefix}{line.content}"
        if not _fits_page(
            candidate,
            used_bytes=used_bytes,
            used_chars=used_chars,
            max_output_chars=max_output_chars,
        ):
            if rendered_lines:
                stream.seek(line_start)
                next_offset = current_line_number
                break
            available_bytes = _MAX_READ_BYTES - len(prefix.encode("utf-8"))
            available_chars = (
                None if max_output_chars is None else max(0, max_output_chars - len(prefix))
            )
            if available_bytes < 0 or available_chars == 0:
                stream.seek(line_start)
                next_offset = current_line_number
                break
            fitted = _fit_utf8_prefix(
                line.content,
                max_bytes=available_bytes,
                max_chars=available_chars,
            )
            candidate = f"{prefix}{fitted}"
            line_truncated = line.truncated or fitted != line.content
        else:
            line_truncated = line.truncated

        rendered_lines.append(candidate.removeprefix(separator))
        used_bytes += len(candidate.encode("utf-8"))
        used_chars += len(candidate)
        current_line_number += 1

        if line_truncated:
            if _has_more_data(stream):
                next_offset = current_line_number
            break

    if next_offset is None and len(rendered_lines) == limit and _has_more_data(stream):
        next_offset = current_line_number

    return _TextPage(
        content="\n".join(rendered_lines),
        line_count=len(rendered_lines),
        next_offset=next_offset,
        line_truncated=line_truncated,
    )


def _skip_lines(stream: TextIO, count: int) -> bool:
    for _ in range(count):
        consumed = False
        while True:
            chunk = stream.readline(_DISCARD_CHUNK_CHARS)
            if not chunk:
                if not consumed:
                    return False
                break
            consumed = True
            if chunk.endswith("\n"):
                break
    return True


def _read_bounded_line(stream: TextIO) -> _TextLine | None:
    raw = stream.readline(_LINE_READ_CHARS)
    if not raw:
        return None
    captured_complete_line = raw.endswith("\n") or len(raw) < _LINE_READ_CHARS
    if not captured_complete_line:
        saw_remainder = False
        while True:
            remainder = stream.readline(_DISCARD_CHUNK_CHARS)
            if not remainder:
                break
            saw_remainder = True
            if remainder.endswith("\n"):
                break
        captured_complete_line = not saw_remainder

    content = raw.removesuffix("\n") if captured_complete_line else raw
    return _TextLine(content=content, truncated=not captured_complete_line)


def _fits_page(
    candidate: str,
    *,
    used_bytes: int,
    used_chars: int,
    max_output_chars: int | None,
) -> bool:
    if used_bytes + len(candidate.encode("utf-8")) > _MAX_READ_BYTES:
        return False
    return max_output_chars is None or used_chars + len(candidate) <= max_output_chars


def _fit_utf8_prefix(
    text: str,
    *,
    max_bytes: int,
    max_chars: int | None,
) -> str:
    if max_chars is not None:
        text = text[:max_chars]
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _has_more_data(stream: TextIO) -> bool:
    position = stream.tell()
    more = bool(stream.read(1))
    stream.seek(position)
    return more


def _to_lf(text: str) -> str:
    """Normalize CRLF and lone CR line endings to LF, mirroring universal-newline reads."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_pure_crlf(text: str) -> bool:
    """Return True when every line feed in the text is part of a CRLF sequence."""
    crlf_count = text.count("\r\n")
    return crlf_count > 0 and text.count("\n") == crlf_count


def _path_arg(invocation: ToolInvocation, context: ToolExecutionContext) -> Path:
    path = invocation.arguments.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("tool argument 'path' must be a non-empty string")
    return context.resolve_path(path)


def _text_arg(invocation: ToolInvocation, name: str) -> str:
    value = invocation.arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"tool argument '{name}' must be a string")
    return value


def _integer_arg(invocation: ToolInvocation, name: str, *, default: int) -> int:
    value = invocation.arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"tool argument '{name}' must be an integer")
    return value


def _boolean_arg(invocation: ToolInvocation, name: str, *, default: bool) -> bool:
    value = invocation.arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"tool argument '{name}' must be a boolean")
    return value


def _write_mode_arg(invocation: ToolInvocation) -> WriteMode:
    value = invocation.arguments.get("mode", "overwrite")
    if value not in ("overwrite", "append"):
        raise ValueError("tool argument 'mode' must be 'overwrite' or 'append'")
    return value
