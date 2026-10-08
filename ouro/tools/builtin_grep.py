"""Built-in Grep tool definition and content search."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin_common import BuiltinArguments, JsonInteger, _tool_result
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


class _GrepArguments(BuiltinArguments):
    pattern: str = Field(
        min_length=1,
        description=("Python regular expression; escape metacharacters to match them literally."),
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "File or root directory, absolute or workspace-relative. Defaults to the "
            "workspace; required if none is configured. An explicit file bypasses ignore "
            "rules and glob/type filters."
        ),
    )
    glob: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Case-sensitive file filter: '*.ts' matches basenames; patterns with / match "
            "absolute paths, so use '**/src/**/*.ts', not 'src/**/*.ts'. Use path to scope "
            "a directory."
        ),
    )
    file_type: str | None = Field(
        default=None,
        alias="type",
        min_length=1,
        description=(
            "File type filter; prefer over glob for languages. Supported: c, cpp, css, go, "
            "html, java, js, json, md, py, rust, sh, sql, toml, ts, txt, xml, yaml."
        ),
    )
    output_mode: Literal["content", "files_with_matches", "count_matches"] = Field(
        default="files_with_matches",
        description=(
            "'files_with_matches' (default): paths, newest modified first. 'content': "
            "matching lines and context. 'count_matches': matching-line counts as "
            "path:count plus totals, not individual regex occurrence counts."
        ),
    )
    after: JsonInteger = Field(
        default=0,
        alias="-A",
        ge=0,
        description=("Context lines after each match; content mode only. Default 0."),
    )
    before: JsonInteger = Field(
        default=0,
        alias="-B",
        ge=0,
        description=("Context lines before each match; content mode only. Default 0."),
    )
    context: JsonInteger | None = Field(
        default=None,
        alias="-C",
        ge=0,
        description=("Context lines on both sides; content mode only. Overrides -A and -B."),
    )
    ignore_case: bool = Field(
        default=False,
        alias="-i",
        description=("Ignore case in content matching. Default false."),
    )
    show_numbers: bool = Field(
        default=True,
        alias="-n",
        description=("Show line numbers in content mode. Default true."),
    )
    head_limit: JsonInteger = Field(
        default=_DEFAULT_HEAD_LIMIT,
        ge=0,
        description=(
            "Maximum entries after offset: matching lines in content mode (excluding "
            "context), files otherwise. Default 250; 0 removes the page limit. Content "
            "collection is capped at 20,000 matching lines."
        ),
    )
    offset: JsonInteger = Field(
        default=0,
        ge=0,
        description=("Entries to skip before head_limit; use for pagination. Default 0."),
    )
    include_ignored: bool = Field(
        default=False,
        description=(
            "Bypass .gitignore, .ignore, and .rgignore when ignored files are needed. "
            "Default false; other exclusions still apply."
        ),
    )
    multiline: bool = Field(
        default=False,
        description=("Allow matches across lines and let . match newlines. Default false."),
    )


def builtin_grep_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name="Grep",
            description=GREP_DESCRIPTION,
            parameters_schema=_GrepArguments.model_json_schema(),
            permission_mode="auto",
            effect="read_only",
        ),
        handler=_grep,
    )


def _grep(
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult:
    arguments = _GrepArguments.model_validate(invocation.arguments)
    pattern = arguments.pattern
    context_before = arguments.context if arguments.context is not None else arguments.before
    context_after = arguments.context if arguments.context is not None else arguments.after
    ignore_case, show_numbers = arguments.ignore_case, arguments.show_numbers
    multiline, include_ignored = arguments.multiline, arguments.include_ignored
    output_mode = arguments.output_mode
    head_limit, offset = arguments.head_limit, arguments.offset
    name_filter = compile_name_glob(arguments.glob) if arguments.glob is not None else None
    type_filter = (
        compile_type_filter(arguments.file_type) if arguments.file_type is not None else None
    )
    target = _path_arg(arguments.path, context)
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
            collect_content=output_mode == "content",
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
            collect_content=output_mode == "content",
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
    return tuple(items[offset : offset + head_limit if head_limit > 0 else None])


def _path_arg(raw: str | None, context: ToolExecutionContext) -> Path:
    if raw is None:
        resolved = context.resolve_working_directory(None)
        if resolved is None:
            raise ValueError("tool argument 'path' is required when no workspace is configured")
        return Path(resolved)
    return Path(context.resolve_path(raw, read_only=True))


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
    collect_content: bool = True,
) -> GrepResult:
    regex = _compile_search_regex(pattern, ignore_case=ignore_case, multiline=multiline)
    entries = (
        entry
        for entry in iter_files(
            root, include_ignored=include_ignored, check_cancelled=check_cancelled
        )
        if (name_filter is None or name_filter.matches(entry))
        and (type_filter is None or _matches_any(type_filter, entry.path.name))
    )
    return _search_entries(
        entries,
        regex,
        multiline=multiline,
        context_before=context_before,
        context_after=context_after,
        collect_content=collect_content,
        check_cancelled=check_cancelled,
    )


def _search_entries(
    entries: Iterable[FileEntry],
    regex: re.Pattern[str],
    *,
    multiline: bool,
    context_before: int,
    context_after: int,
    collect_content: bool,
    check_cancelled: Callable[[], None] | None,
) -> GrepResult:
    matches: list[MatchedLine] = []
    file_counts: list[FileMatchCount] = []
    total = 0
    for entry in entries:
        if check_cancelled is not None:
            check_cancelled()
        text = _read_searchable_text(entry.path)
        if check_cancelled is not None:
            check_cancelled()
        if text is None:
            continue
        lines = text.splitlines()
        count = 0
        for number in _matched_line_numbers(lines, regex, multiline=multiline):
            count += 1
            if collect_content and len(matches) < _MAX_COLLECTED_MATCHES:
                matches.append(
                    MatchedLine(
                        entry=entry,
                        number=number,
                        text=lines[number - 1],
                        before=_context(lines, number, -context_before),
                        after=_context(lines, number, context_after),
                    )
                )
        if count:
            file_counts.append(FileMatchCount(entry=entry, count=count))
            total += count
    return GrepResult(
        matches=tuple(matches),
        total=total,
        collection_capped=collect_content and total > len(matches),
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
    collect_content: bool = True,
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
    return _search_entries(
        (entry,),
        regex,
        multiline=multiline,
        context_before=context_before,
        context_after=context_after,
        collect_content=collect_content,
        check_cancelled=check_cancelled,
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
) -> Iterator[int]:
    if not multiline:
        yield from (index + 1 for index, line in enumerate(lines) if regex.search(line) is not None)
        return
    if not lines:
        return
    offsets = _line_start_offsets(lines)
    matched: set[int] = set()
    text = "\n".join(lines)
    for hit in regex.finditer(text):
        first = bisect_right(offsets, hit.start())
        last = bisect_right(offsets, max(hit.start(), hit.end() - 1))
        matched.update(range(first, last + 1))
    yield from sorted(matched)


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
            content = bytearray(head)
            while chunk := stream.read(64 * 1024):
                content.extend(chunk)
                if len(content) > _MAX_SEARCHABLE_FILE_BYTES:
                    return None
        return content.decode("utf-8", errors="replace")
    except OSError:
        return None


__all__ = ["GREP_DESCRIPTION", "builtin_grep_tool"]
