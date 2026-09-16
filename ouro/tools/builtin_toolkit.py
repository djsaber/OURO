"""Built-in Glob, Grep, and TodoList tool definitions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from ouro.core.base import utc_now
from ouro.core.harness_calls import HarnessInvocation, HarnessStateChange
from ouro.tools.base import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolResult,
)
from ouro.tools.builtin_todos import TodoItem, TodoListRepository
from ouro.tools.builtin_toolkit_ignore import is_sensitive_file
from ouro.tools.builtin_toolkit_search import (
    FileEntry,
    GrepResult,
    MatchedLine,
    compile_name_glob,
    compile_type_filter,
    grep_single_file,
    search_glob,
    search_grep,
)

_GLOB_MAX_RESULTS = 100
_DEFAULT_HEAD_LIMIT = 250
_TODOLIST_STATE_OWNER = "ouro.host"

GLOB_DESCRIPTION = """Use this tool to discover files when you know a filename, extension, or \
directory pattern but not the exact path. Prefer Grep when you need to locate file content and \
Read when you already know the file path. Do not use this tool to list directories or search \
inside files. Results are sorted by modification time (most recent first).

Respects `.gitignore`, `.ignore`, and `.rgignore` by default — set `include_ignored` to also \
match ignored files (e.g. build outputs, `node_modules`). Sensitive files (such as `.env`) are \
always filtered out. Matches are files only — directories themselves are never listed; to find \
a directory, glob for a file inside it (e.g. `**/fixtures/**`). Good patterns:

- `*.ts` — all files matching an extension, at any depth below the search root (a bare pattern \
without `/` matches recursively)
- `src/*.ts` — files directly inside `src/` (one level, not recursive)
- `src/**/*.ts` — recursive walk with a subdirectory anchor and extension
- `**/*.py` — recursive walk from the search root for an extension
- `*.{ts,tsx}` — brace expansion is supported
- `{src,test}/**/*.ts` — cartesian brace expansion is supported too

Results are capped at the first 100 matching paths. If a search would return more, a truncation \
marker is appended. Refine the pattern (extension, subdirectory) when 100 is not enough, or \
call again with a narrower anchor.

Large-directory caveat — avoid recursing into dependency / build output even with an anchor, \
especially when `include_ignored` is set: `node_modules/**/*.js`, `.venv/**/*.py`, \
`__pycache__/**`, `target/**` can produce thousands of results that truncate at the match cap \
and waste context. Prefer specific subpaths like `node_modules/react/src/**/*.js`."""

GREP_DESCRIPTION = """Use this tool to locate regular-expression matches when the relevant \
content or file location is unknown. Prefer Glob when you only need filenames and Read when you \
already know the exact file and range. Do not read whole files into context when a targeted \
search can locate the relevant lines.

Write patterns in ripgrep regex syntax, which differs from POSIX `grep` syntax. For example, \
braces are special, so escape them as `\\{` to match a literal `{`.

Hidden files (dotfiles such as `.gitlab-ci.yml` or `.eslintrc.json`) are searched by default. \
To also search files excluded by `.gitignore` (such as `node_modules` or build outputs), set \
`include_ignored` to `true`. Sensitive files (such as `.env`) are always skipped for safety, \
even when `include_ignored` is `true`."""

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

_GLOB_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Glob pattern selecting the files you need. A bare filename pattern such as "
                "`*.py` matches recursively; include `/` to anchor the pattern to subpaths."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "Root directory below which to match files. Use an absolute path or a path "
                "relative to the current working directory. Omit to use the current working "
                "directory."
            ),
        },
        "include_ignored": {
            "type": "boolean",
            "description": (
                "Set to true only when you specifically need files excluded by `.gitignore`, "
                "`.ignore`, or `.rgignore`, such as a dependency or build output. Sensitive "
                "files (such as `.env`) remain filtered out for safety. VCS metadata "
                "directories (`.git` and similar) are always skipped, even when this is true. "
                "Defaults to false."
            ),
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}

_GREP_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Ripgrep-compatible regular expression to find. Escape regex metacharacters "
                "when you intend to match them literally."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "Exact file or root directory to search. Use an absolute path or a path "
                "relative to the current working directory. Omit to search the current "
                "working directory."
            ),
        },
        "glob": {
            "type": "string",
            "description": (
                "Optional glob restricting which files are searched, for example `*.ts`. It "
                "is matched against each file's full absolute path, so a path-anchored "
                "pattern like "
                "`src/**/*.ts` silently matches nothing — use a basename pattern (`*.ts`), "
                "or anchor with `**/` (`**/src/**/*.ts`). To scope the search to a "
                "directory, use `path` instead."
            ),
        },
        "type": {
            "type": "string",
            "description": (
                "Optional ripgrep file type, such as `ts` or `py`. Prefer this over `glob` when "
                "filtering by language or file kind: it is more efficient and less "
                "error-prone than an equivalent glob pattern."
            ),
        },
        "output_mode": {
            "type": "string",
            "enum": ["content", "files_with_matches", "count_matches"],
            "description": (
                "Choose `files_with_matches` for file discovery, `content` for matching lines "
                "and context, or `count_matches` for per-file counts. `content` honors `-A`, `-B`, "
                "`-C`, `-n`, and `head_limit`; `files_with_matches` shows only the paths of "
                "files that contain a match, most-recently-modified first (honors "
                "`head_limit`); `count_matches` shows per-file match counts as `path:count` "
                "lines, preceded by an aggregate total line. Defaults to "
                "`files_with_matches`."
            ),
        },
        "-A": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Number of lines to show after each match. Applies only when `output_mode` "
                "is `content`."
            ),
        },
        "-B": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Number of lines to show before each match. Applies only when `output_mode` "
                "is `content`."
            ),
        },
        "-C": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Number of lines to show before and after each match. Applies only when "
                "`output_mode` is `content`; takes precedence over `-A` and `-B`."
            ),
        },
        "-i": {
            "type": "boolean",
            "description": "Perform a case-insensitive search. Defaults to false.",
        },
        "-n": {
            "type": "boolean",
            "description": (
                "Prefix each matching line with its line number. Applies only when "
                "`output_mode` is `content`. Defaults to true."
            ),
        },
        "head_limit": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Limit output to the first N lines/entries after offset. Defaults to 250. "
                "Pass 0 for unlimited."
            ),
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Number of leading lines/entries to skip before applying `head_limit`. Use "
                "it together with `head_limit` to page through large result sets. Defaults "
                "to 0."
            ),
        },
        "include_ignored": {
            "type": "boolean",
            "description": (
                "Also search files excluded by ignore files such as `.gitignore`, `.ignore`, "
                "and `.rgignore` (for example `node_modules` or build outputs). Sensitive "
                "files (such as `.env`) are always skipped for safety, even when "
                "`include_ignored` is `true`. VCS metadata directories (`.git` and similar) "
                "are always skipped, even when this is true. Defaults to false."
            ),
        },
        "multiline": {
            "type": "boolean",
            "description": (
                "Enable multiline matching, where the pattern can span line boundaries and "
                "`.` also matches newlines. Defaults to false."
            ),
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}

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


def builtin_toolkit_tools() -> list[Tool]:
    return [
        Tool(
            definition=ToolDefinition(
                name="Glob",
                description=GLOB_DESCRIPTION,
                parameters_schema=dict(_GLOB_SCHEMA),
                permission_mode="auto",
                effect="read_only",
            ),
            handler=_glob,
        ),
        Tool(
            definition=ToolDefinition(
                name="Grep",
                description=GREP_DESCRIPTION,
                parameters_schema=dict(_GREP_SCHEMA),
                permission_mode="auto",
                effect="read_only",
            ),
            handler=_grep,
        ),
        Tool(
            definition=ToolDefinition(
                name="TodoList",
                description=TODOLIST_DESCRIPTION,
                parameters_schema=dict(_TODOLIST_SCHEMA),
                permission_mode="auto",
                effect="state_changing",
                batch_behavior="exclusive",
            ),
            handler=_todolist,
        ),
    ]


def _glob(
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult:
    pattern = _string_arg(invocation.arguments, "pattern")
    include_ignored = _boolean_arg(invocation.arguments, "include_ignored", default=False)
    root = _directory_arg(invocation.arguments, context)
    result = search_glob(
        root,
        pattern,
        include_ignored=include_ignored,
        limit=_GLOB_MAX_RESULTS,
        check_cancelled=context.raise_if_aborted,
    )
    paths = [entry.relative for entry in result.matches]
    if not paths:
        content = "No files found."
    else:
        lines: list[str] = []
        if result.total > _GLOB_MAX_RESULTS:
            lines.append(
                f"[Truncated at {_GLOB_MAX_RESULTS} matches — use a more specific pattern]"
            )
            lines.append(f"Only the first {_GLOB_MAX_RESULTS} matches are returned.")
        lines.extend(paths)
        content = "\n".join(lines)
    return _tool_result(
        invocation,
        content=content,
        structured={
            "kind": "glob",
            "root": str(root),
            "pattern": pattern,
            "matches": paths,
            "total": result.total,
            "truncated": result.total > _GLOB_MAX_RESULTS,
        },
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


def _tool_result(
    invocation: ToolInvocation,
    *,
    content: str,
    structured: dict[str, object] | None = None,
    is_error: bool = False,
    harness_change: HarnessStateChange | None = None,
) -> ToolResult:
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=content,
        structured=structured or {},
        is_error=is_error,
        harness_change=harness_change,
        ended_at=utc_now(),
    )


def _string_arg(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"tool argument '{name}' must be a non-empty string")
    return value


def _optional_string_arg(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"tool argument '{name}' must be a non-empty string")
    return value


def _boolean_arg(arguments: Mapping[str, object], name: str, *, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"tool argument '{name}' must be a boolean")
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


def _directory_arg(arguments: Mapping[str, object], context: ToolExecutionContext) -> Path:
    raw = arguments.get("path")
    if raw is None:
        resolved = context.resolve_working_directory(None)
        root = Path(resolved) if resolved is not None else Path.cwd()
    else:
        if not isinstance(raw, str) or not raw:
            raise ValueError("tool argument 'path' must be a non-empty string")
        root = Path(context.resolve_path(raw))
    if not root.is_dir():
        raise ValueError(f"search root is not a directory: {root}")
    return root


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


__all__ = [
    "GLOB_DESCRIPTION",
    "GREP_DESCRIPTION",
    "TODOLIST_DESCRIPTION",
    "builtin_toolkit_tools",
]
