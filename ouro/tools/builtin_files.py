from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TextIO

from pydantic import Field

from ouro.core.base import utc_now
from ouro.tools.base import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolResult,
)
from ouro.tools.builtin_common import BuiltinArguments, JsonInteger

_MAX_READ_LINES = 1_000
_MAX_READ_BYTES = 100_000
_LINE_READ_CHARS = _MAX_READ_BYTES + 1
_DISCARD_CHUNK_CHARS = 64 * 1_024


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


class _ReadArguments(BuiltinArguments):
    path: str = Field(
        min_length=1,
        description=("Absolute or workspace-relative file path."),
    )
    offset: JsonInteger = Field(
        default=1,
        ge=1,
        description=(
            "1-based starting line; use next_offset from a previous result to continue reading."
        ),
    )
    limit: JsonInteger = Field(
        default=_MAX_READ_LINES,
        ge=1,
        le=_MAX_READ_LINES,
        description=("Maximum lines to return; the byte cap may shorten the page."),
    )


class _WriteArguments(BuiltinArguments):
    path: str = Field(
        min_length=1,
        description=("Absolute or workspace-relative destination file path."),
    )
    content: str = Field(
        description=("Exact text to write; no text or newlines are added."),
    )
    mode: Literal["overwrite", "append"] = Field(
        default="overwrite",
        description=(
            "'overwrite' replaces the entire file; 'append' adds to its end. Both create a "
            "missing file."
        ),
    )


class _EditArguments(BuiltinArguments):
    path: str = Field(
        min_length=1,
        description=("Absolute or workspace-relative path of the existing file."),
    )
    old: str = Field(
        min_length=1,
        description=(
            "Exact text copied from the file, without Read's line-number prefixes. "
            "Include enough surrounding text to identify the intended occurrence; not regex."
        ),
    )
    new: str = Field(
        description=("Replacement text; an empty string deletes the match."),
    )
    replace_all: bool = Field(
        default=False,
        description=("Set true only to replace every occurrence; false replaces only the first."),
    )


def builtin_file_tools() -> list[Tool]:
    return [
        Tool(
            definition=ToolDefinition(
                name="Read",
                description=(
                    "Read a known UTF-8 text file with line numbers. Use Glob to find paths "
                    "or Grep to search content. Directories, binary files, and non-UTF-8 files "
                    "are unsupported. Returns '<line number>\\t<content>', capped at 1,000 "
                    "lines or 100,000 UTF-8 bytes per call."
                ),
                parameters_schema=_ReadArguments.model_json_schema(),
                permission_mode="auto",
                effect="read_only",
            ),
            handler=_read,
        ),
        Tool(
            definition=ToolDefinition(
                name="Write",
                description=(
                    "Create, overwrite, or append to a UTF-8 text file. Prefer Edit for "
                    "targeted changes. Read or verify existing content before overwriting "
                    "a file whose contents matter. Creates missing parent directories."
                ),
                parameters_schema=_WriteArguments.model_json_schema(),
                permission_mode="approval",
                effect="state_changing",
            ),
            handler=_write,
        ),
        Tool(
            definition=ToolDefinition(
                name="Edit",
                description=(
                    "Replace exact text in an existing UTF-8 file. Call with path, old, and new; "
                    "replace_all is optional. Read the relevant section first. Whitespace must "
                    "match except line endings, which are normalized to LF; CRLF files retain "
                    "CRLF. Replaces the first occurrence unless replace_all is true. If old is "
                    "absent, the file is unchanged. Use Write for new files or full rewrites."
                ),
                parameters_schema=_EditArguments.model_json_schema(),
                permission_mode="approval",
                effect="state_changing",
            ),
            handler=_edit,
        ),
    ]


def _read(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
    arguments = _ReadArguments.model_validate(invocation.arguments)
    path = context.resolve_path(arguments.path, read_only=True)
    if path.is_dir():
        raise IsADirectoryError(f"path is a directory, not a text file: {path}")
    offset, limit = arguments.offset, arguments.limit
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


def _write(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
    arguments = _WriteArguments.model_validate(invocation.arguments)
    path = context.resolve_path(arguments.path)
    content, mode = arguments.content, arguments.mode
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


def _edit(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
    arguments = _EditArguments.model_validate(invocation.arguments)
    path = context.resolve_path(arguments.path)
    old, new = _to_lf(arguments.old), _to_lf(arguments.new)
    replace_all = arguments.replace_all
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
            content=(
                f"No changes to {path}: old was not found. Read the current file, then copy "
                "the exact target text into old without line-number prefixes. Check spaces "
                "and indentation; line endings are normalized automatically."
            ),
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
        content=(
            f"Edited {path}; replaced {replacements} occurrence(s)"
            if updated != text
            else f"No changes to {path}: old and new are identical after normalizing line endings."
        ),
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
