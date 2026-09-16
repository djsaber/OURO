"""Built-in Glob tool definition and filename search."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin_common import _boolean_arg, _string_arg, _tool_result
from ouro.tools.builtin_search import FileEntry, _matches_any, _translate, iter_files

_GLOB_MAX_RESULTS = 100


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


def builtin_glob_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name="Glob",
            description=GLOB_DESCRIPTION,
            parameters_schema=dict(_GLOB_SCHEMA),
            permission_mode="auto",
            effect="read_only",
        ),
        handler=_glob,
    )


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


@dataclass(frozen=True)
class GlobResult:
    matches: tuple[FileEntry, ...]  # most recently modified first, capped at limit
    total: int


def compile_path_glob(pattern: str) -> tuple[re.Pattern[str], ...]:
    """Compile one Glob-tool pattern into anchored regexes over relative POSIX paths.

    A bare pattern without a path separator matches recursively at any depth,
    so ``*.py`` is equivalent to ``**/*.py``.
    """

    normalized = pattern if "/" in pattern else f"**/{pattern}"
    return _translate(normalized)


def search_glob(
    root: Path,
    pattern: str,
    *,
    include_ignored: bool,
    limit: int,
    check_cancelled: Callable[[], None] | None = None,
) -> GlobResult:
    matchers = compile_path_glob(pattern)
    found = [
        entry
        for entry in iter_files(
            root,
            include_ignored=include_ignored,
            check_cancelled=check_cancelled,
        )
        if _matches_any(matchers, entry.relative)
    ]
    found.sort(key=lambda entry: (-entry.mtime, entry.relative))
    return GlobResult(matches=tuple(found[:limit]), total=len(found))


__all__ = ["GLOB_DESCRIPTION", "builtin_glob_tool"]
