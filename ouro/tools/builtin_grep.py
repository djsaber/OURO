"""Built-in Grep tool definition and content search."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin_common import _boolean_arg, _string_arg, _tool_result
from ouro.tools.builtin_ignore import is_sensitive_file
from ouro.tools.builtin_search import FileEntry, _matches_any, _translate, iter_files

_DEFAULT_HEAD_LIMIT = 250


_BINARY_SNIFF_BYTES = 8 * 1_024


_MAX_SEARCHABLE_FILE_BYTES = 32 * 1_024 * 1_024


# Bound content-mode result memory; counting continues for an exact match total.
_MAX_COLLECTED_MATCHES = 20_000


# Small ripgrep-style file type table for the Grep `type` filter.
_FILE_TYPES: dict[str, tuple[str, ...]] = {
    "c": ("*.c", "*.h"),
    "cpp": ("*.cpp", "*.cc", "*.cxx", "*.hpp", "*.hh", "*.hxx"),
    "css": ("*.css",),
    "go": ("*.go",),
    "html": ("*.html", "*.htm"),
    "java": ("*.java",),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "json": ("*.json",),
    "md": ("*.md", "*.markdown"),
    "py": ("*.py", "*.pyi"),
    "rust": ("*.rs",),
    "sh": ("*.sh", "*.bash", "*.zsh"),
    "sql": ("*.sql",),
    "toml": ("*.toml",),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "txt": ("*.txt",),
    "xml": ("*.xml",),
    "yaml": ("*.yaml", "*.yml"),
}


GREP_DESCRIPTION = """\
Search file contents by regex to locate relevant files or lines. Use Glob for filenames and
Read for a known file range. Includes hidden files; skips .env, .env.*, binary or unreadable
files, and files over 32 MiB. Directory searches also skip version-control metadata and
symlinks. Narrow the search if results are truncated."""


_GREP_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Python regular expression; escape metacharacters to match them literally."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "File or root directory, absolute or workspace-relative. Defaults to the "
                "workspace; required if none is configured. An explicit file bypasses ignore "
                "rules and glob/type filters."
            ),
        },
        "glob": {
            "type": "string",
            "description": (
                "Case-sensitive file filter: '*.ts' matches basenames; patterns with / match "
                "absolute paths, so use '**/src/**/*.ts', not 'src/**/*.ts'. Use path to "
                "scope a directory."
            ),
        },
        "type": {
            "type": "string",
            "description": (
                "File type filter; prefer over glob for languages. Supported: c, cpp, css, "
                "go, html, java, js, json, md, py, rust, sh, sql, toml, ts, txt, xml, yaml."
            ),
        },
        "output_mode": {
            "type": "string",
            "enum": ["content", "files_with_matches", "count_matches"],
            "description": (
                "'files_with_matches' (default): paths, newest modified first. 'content': "
                "matching lines and context. 'count_matches': matching-line counts as "
                "path:count plus totals, not individual regex occurrence counts."
            ),
        },
        "-A": {
            "type": "integer",
            "minimum": 0,
            "description": "Context lines after each match; content mode only. Default 0.",
        },
        "-B": {
            "type": "integer",
            "minimum": 0,
            "description": "Context lines before each match; content mode only. Default 0.",
        },
        "-C": {
            "type": "integer",
            "minimum": 0,
            "description": "Context lines on both sides; content mode only. Overrides -A and -B.",
        },
        "-i": {
            "type": "boolean",
            "description": "Ignore case in content matching. Default false.",
        },
        "-n": {
            "type": "boolean",
            "description": "Show line numbers in content mode. Default true.",
        },
        "head_limit": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Maximum entries after offset: matching lines in content mode (excluding "
                "context), files otherwise. Default 250; 0 removes the page limit. Content "
                "collection is capped at 20,000 matching lines."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Entries to skip before head_limit; use for pagination. Default 0.",
        },
        "include_ignored": {
            "type": "boolean",
            "description": (
                "Bypass .gitignore, .ignore, and .rgignore when ignored files are needed. "
                "Default false; other exclusions still apply."
            ),
        },
        "multiline": {
            "type": "boolean",
            "description": "Allow matches across lines and let . match newlines. Default false.",
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}


def builtin_grep_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name="Grep",
            description=GREP_DESCRIPTION,
            parameters_schema=dict(_GREP_SCHEMA),
            permission_mode="auto",
            effect="read_only",
        ),
        handler=_grep,
    )


def _grep(
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult:
    arguments = invocation.arguments
    pattern = _string_arg(arguments, "pattern")
    context_span = _optional_int_arg(arguments, "-C")
    context_before = (
        context_span if context_span is not None else _optional_int_arg(arguments, "-B") or 0
    )
    context_after = (
        context_span if context_span is not None else _optional_int_arg(arguments, "-A") or 0
    )
    ignore_case = _boolean_arg(arguments, "-i", default=False)
    show_numbers = _boolean_arg(arguments, "-n", default=True)
    multiline = _boolean_arg(arguments, "multiline", default=False)
    include_ignored = _boolean_arg(arguments, "include_ignored", default=False)
    output_mode = _output_mode_arg(arguments)
    head_limit = _int_arg(arguments, "head_limit", default=_DEFAULT_HEAD_LIMIT)
    offset = _int_arg(arguments, "offset", default=0)

    name_filter = None
    raw_glob = _optional_string_arg(arguments, "glob")
    if raw_glob is not None:
        name_filter = compile_name_glob(raw_glob)
    type_filter = None
    raw_type = _optional_string_arg(arguments, "type")
    if raw_type is not None:
        type_filter = compile_type_filter(raw_type)

    target = _path_arg(arguments, context)
    if target.is_file():
        if is_sensitive_file(target.name):
            return _tool_result(
                invocation,
                content="Grep does not search sensitive files such as .env.",
                is_error=True,
            )
        result = grep_single_file(
            target,
            pattern,
            ignore_case=ignore_case,
            multiline=multiline,
            context_before=context_before,
            context_after=context_after,
            check_cancelled=context.raise_if_aborted,
        )
    else:
        if not target.is_dir():
            raise ValueError(f"search path is not a file or directory: {target}")
        result = search_grep(
            target,
            pattern,
            ignore_case=ignore_case,
            multiline=multiline,
            name_filter=name_filter,
            type_filter=type_filter,
            include_ignored=include_ignored,
            context_before=context_before,
            context_after=context_after,
            check_cancelled=context.raise_if_aborted,
        )
    return _render_grep(
        invocation,
        result,
        output_mode=output_mode,
        show_numbers=show_numbers,
        head_limit=head_limit,
        offset=offset,
    )


def _render_grep(
    invocation: ToolInvocation,
    result: GrepResult,
    *,
    output_mode: Literal["content", "files_with_matches", "count_matches"],
    show_numbers: bool,
    head_limit: int,
    offset: int,
) -> ToolResult:
    if result.total == 0:
        return _tool_result(
            invocation,
            content="No matches found.",
            structured={"kind": "grep", "mode": output_mode, "total": 0},
        )
    if output_mode == "files_with_matches":
        return _render_files_with_matches(
            invocation,
            result,
            head_limit=head_limit,
            offset=offset,
        )
    if output_mode == "count_matches":
        return _render_count_matches(
            invocation,
            result,
            head_limit=head_limit,
            offset=offset,
        )
    return _render_content(
        invocation,
        result,
        show_numbers=show_numbers,
        head_limit=head_limit,
        offset=offset,
    )


def _render_files_with_matches(
    invocation: ToolInvocation,
    result: GrepResult,
    *,
    head_limit: int,
    offset: int,
) -> ToolResult:
    ranked = sorted(
        result.file_counts,
        key=lambda item: (-item.entry.mtime, item.entry.relative),
    )
    page = _page(ranked, head_limit=head_limit, offset=offset)
    content = "\n".join(item.entry.relative for item in page)
    return _tool_result(
        invocation,
        content=content,
        structured={
            "kind": "grep",
            "mode": "files_with_matches",
            "files": [item.entry.relative for item in page],
            "total_files": len(ranked),
        },
    )


def _render_count_matches(
    invocation: ToolInvocation,
    result: GrepResult,
    *,
    head_limit: int,
    offset: int,
) -> ToolResult:
    counts = sorted(result.file_counts, key=lambda item: item.entry.relative)
    page = _page(counts, head_limit=head_limit, offset=offset)
    lines = [f"Found {result.total} matches in {len(counts)} files."]
    lines.extend(f"{item.entry.relative}:{item.count}" for item in page)
    return _tool_result(
        invocation,
        content="\n".join(lines),
        structured={
            "kind": "grep",
            "mode": "count_matches",
            "total": result.total,
            "files": [{"path": item.entry.relative, "count": item.count} for item in page],
        },
    )


def _render_content(
    invocation: ToolInvocation,
    result: GrepResult,
    *,
    show_numbers: bool,
    head_limit: int,
    offset: int,
) -> ToolResult:
    page = _page(result.matches, head_limit=head_limit, offset=offset)
    lines: list[str] = []
    for entry_matches in _group_by_file(page):
        lines.extend(_render_file_block(entry_matches, show_numbers=show_numbers))
    emitted = offset + len(page)
    if emitted < result.total:
        limit_note = head_limit if head_limit > 0 else len(page)
        lines.append(
            f"Results truncated to {limit_note} lines (total: {result.total}). "
            f"Use offset={emitted} to see more."
        )
    if result.collection_capped:
        lines.append("Result collection stopped early; refine the pattern to narrow the search.")
    return _tool_result(
        invocation,
        content="\n".join(lines),
        structured={
            "kind": "grep",
            "mode": "content",
            "matches": [
                {"path": match.entry.relative, "line": match.number, "text": match.text}
                for match in page
            ],
            "total": result.total,
            "truncated": emitted < result.total,
        },
    )


def _group_by_file(matches: tuple[MatchedLine, ...] | list[MatchedLine]):
    groups: list[tuple[FileEntry, list[MatchedLine]]] = []
    for match in matches:
        if not groups or groups[-1][0].relative != match.entry.relative:
            groups.append((match.entry, []))
        groups[-1][1].append(match)
    return groups


def _render_file_block(
    group: tuple[FileEntry, list[MatchedLine]],
    *,
    show_numbers: bool,
) -> list[str]:
    entry, matches = group
    # Merge matches and their context lines into per-line records so
    # overlapping context is emitted exactly once; a matched line wins over
    # context at the same line number.
    records: dict[int, tuple[str, str]] = {}
    for match in matches:
        for number, text in match.before:
            records.setdefault(number, ("context", text))
        records[match.number] = ("match", match.text)
        for number, text in match.after:
            records.setdefault(number, ("context", text))

    lines: list[str] = []
    previous: int | None = None
    for number in sorted(records):
        if previous is not None and number > previous + 1:
            lines.append("--")
        kind, text = records[number]
        separator = ":" if kind == "match" else "-"
        label = f"{entry.relative}{separator}{number}" if show_numbers else entry.relative
        lines.append(f"{label}{separator}{text}")
        previous = number
    return lines


def _page(items, *, head_limit: int, offset: int):
    if offset >= len(items):
        return ()
    selected = items[offset:]
    if head_limit > 0:
        selected = selected[:head_limit]
    return tuple(selected)


def _optional_string_arg(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"tool argument '{name}' must be a non-empty string")
    return value


def _int_arg(arguments: Mapping[str, object], name: str, *, default: int) -> int:
    value = arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"tool argument '{name}' must be a non-negative integer")
    return value


def _optional_int_arg(arguments: Mapping[str, object], name: str) -> int | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"tool argument '{name}' must be a non-negative integer")
    return value


def _output_mode_arg(
    arguments: Mapping[str, object],
) -> Literal["content", "files_with_matches", "count_matches"]:
    value = arguments.get("output_mode", "files_with_matches")
    if value not in ("content", "files_with_matches", "count_matches"):
        raise ValueError(
            "tool argument 'output_mode' must be 'content', 'files_with_matches', "
            "or 'count_matches'"
        )
    return cast("Literal['content', 'files_with_matches', 'count_matches']", value)


def _path_arg(arguments: Mapping[str, object], context: ToolExecutionContext) -> Path:
    raw = arguments.get("path")
    if raw is None:
        resolved = context.resolve_working_directory(None)
        if resolved is None:
            raise ValueError("tool argument 'path' is required when no workspace is configured")
        return Path(resolved)
    if not isinstance(raw, str) or not raw:
        raise ValueError("tool argument 'path' must be a non-empty string")
    return Path(context.resolve_path(raw))


@dataclass(frozen=True)
class MatchedLine:
    entry: FileEntry
    number: int  # 1-based line number
    text: str
    before: tuple[tuple[int, str], ...] = ()
    after: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class FileMatchCount:
    entry: FileEntry
    count: int


@dataclass(frozen=True)
class GrepResult:
    matches: tuple[MatchedLine, ...]  # deterministic walk order, bounded by the safety valve
    total: int  # exact matched-line count across all searched files
    collection_capped: bool
    file_counts: tuple[FileMatchCount, ...]  # only files with at least one match


@dataclass(frozen=True)
class NameGlob:
    """Compiled ripgrep-style ``--glob`` filter.

    A pattern without a path separator matches only the file basename; a
    pattern containing one is matched against the file's full POSIX path.
    """

    matchers: tuple[re.Pattern[str], ...]
    anchored: bool

    def matches(self, entry: FileEntry) -> bool:
        subject = entry.path.as_posix() if self.anchored else entry.path.name
        return _matches_any(self.matchers, subject)


def compile_name_glob(pattern: str) -> NameGlob:
    return NameGlob(matchers=_translate(pattern), anchored="/" in pattern)


def supported_file_types() -> tuple[str, ...]:
    return tuple(sorted(_FILE_TYPES))


def compile_type_filter(file_type: str) -> tuple[re.Pattern[str], ...]:
    patterns = _FILE_TYPES.get(file_type)
    if patterns is None:
        supported = ", ".join(supported_file_types())
        raise ValueError(f"unsupported file type '{file_type}' (supported: {supported})")
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        compiled.extend(_translate(pattern))
    return tuple(compiled)


def search_grep(
    root: Path,
    pattern: str,
    *,
    ignore_case: bool,
    multiline: bool,
    name_filter: NameGlob | None,
    type_filter: tuple[re.Pattern[str], ...] | None,
    include_ignored: bool,
    context_before: int,
    context_after: int,
    check_cancelled: Callable[[], None] | None = None,
) -> GrepResult:
    regex = _compile_search_regex(pattern, ignore_case=ignore_case, multiline=multiline)
    matches: list[MatchedLine] = []
    file_counts: list[FileMatchCount] = []
    total = 0
    collection_capped = False
    for entry in iter_files(
        root,
        include_ignored=include_ignored,
        check_cancelled=check_cancelled,
    ):
        if name_filter is not None and not name_filter.matches(entry):
            continue
        if type_filter is not None and not _matches_any(type_filter, entry.path.name):
            continue
        text = _read_searchable_text(entry.path)
        if text is None:
            continue
        lines = text.splitlines()
        matched_numbers = _matched_line_numbers(lines, regex, multiline=multiline)
        if not matched_numbers:
            continue
        file_counts.append(FileMatchCount(entry=entry, count=len(matched_numbers)))
        total += len(matched_numbers)
        for number in matched_numbers:
            if len(matches) >= _MAX_COLLECTED_MATCHES:
                collection_capped = True
                break
            matches.append(
                MatchedLine(
                    entry=entry,
                    number=number,
                    text=lines[number - 1],
                    before=_context(lines, number, -context_before),
                    after=_context(lines, number, context_after),
                )
            )
    return GrepResult(
        matches=tuple(matches),
        total=total,
        collection_capped=collection_capped,
        file_counts=tuple(file_counts),
    )


def grep_single_file(
    path: Path,
    pattern: str,
    *,
    ignore_case: bool,
    multiline: bool,
    context_before: int,
    context_after: int,
    check_cancelled: Callable[[], None] | None = None,
) -> GrepResult:
    """Search one explicitly named file; ignore rules and glob filters do not apply."""

    if check_cancelled is not None:
        check_cancelled()
    regex = _compile_search_regex(pattern, ignore_case=ignore_case, multiline=multiline)
    try:
        stat = path.stat()
    except OSError:
        stat = None
    entry = FileEntry(
        path=path,
        relative=path.name,
        mtime=stat.st_mtime if stat is not None else 0.0,
    )
    text = _read_searchable_text(path)
    if check_cancelled is not None:
        check_cancelled()
    if text is None:
        return GrepResult(matches=(), total=0, collection_capped=False, file_counts=())
    lines = text.splitlines()
    matched_numbers = _matched_line_numbers(lines, regex, multiline=multiline)
    matches = tuple(
        MatchedLine(
            entry=entry,
            number=number,
            text=lines[number - 1],
            before=_context(lines, number, -context_before),
            after=_context(lines, number, context_after),
        )
        for number in matched_numbers[:_MAX_COLLECTED_MATCHES]
    )
    file_counts = (
        (FileMatchCount(entry=entry, count=len(matched_numbers)),) if matched_numbers else ()
    )
    return GrepResult(
        matches=matches,
        total=len(matched_numbers),
        collection_capped=len(matched_numbers) > _MAX_COLLECTED_MATCHES,
        file_counts=file_counts,
    )


def _compile_search_regex(pattern: str, *, ignore_case: bool, multiline: bool) -> re.Pattern[str]:
    flags = re.IGNORECASE if ignore_case else 0
    if multiline:
        flags |= re.MULTILINE | re.DOTALL
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc


def _matched_line_numbers(
    lines: list[str],
    regex: re.Pattern[str],
    *,
    multiline: bool,
) -> list[int]:
    if not multiline:
        return [index + 1 for index, line in enumerate(lines) if regex.search(line) is not None]
    offsets = _line_start_offsets(lines)
    matched: set[int] = set()
    text = "\n".join(lines)
    for hit in regex.finditer(text):
        first = bisect_right(offsets, hit.start())
        last = bisect_right(offsets, max(hit.start(), hit.end() - 1))
        matched.update(range(first, last + 1))
    return sorted(matched)


def _line_start_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    return offsets


def _context(lines: list[str], number: int, span: int) -> tuple[tuple[int, str], ...]:
    if span == 0:
        return ()
    if span < 0:
        start = max(1, number + span)
        stop = number
    else:
        start = number + 1
        stop = min(len(lines), number + span) + 1
    return tuple((index, lines[index - 1]) for index in range(start, stop))


def _read_searchable_text(path: Path) -> str | None:
    """Decode a file as UTF-8 text, returning ``None`` for binary or unreadable files."""

    try:
        with path.open("rb") as stream:
            head = stream.read(_BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                return None
            rest = stream.read(_MAX_SEARCHABLE_FILE_BYTES + 1 - len(head))
            if len(rest) + len(head) > _MAX_SEARCHABLE_FILE_BYTES:
                return None
        return (head + rest).decode("utf-8", errors="replace")
    except OSError:
        return None


__all__ = ["GREP_DESCRIPTION", "builtin_grep_tool"]
