"""Built-in Glob tool definition and filename search."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin.common import BuiltinArguments, _tool_result
from ouro.tools.builtin.search import FileEntry, _matches_any, _translate, iter_files

_GLOB_MAX_RESULTS = 100


GLOB_DESCRIPTION = """\
Find files by name or path pattern. Use Grep for content searches and Read for known files.
Returns up to 100 paths relative to the search root, newest modified first; never lists
directories. To locate a directory, match files inside it (e.g. '**/fixtures/**').
Narrow truncated searches; use specific subpaths in dependency or build directories.
Includes hidden files; always skips .env, .env.*, version-control metadata, and symlinks."""


class _GlobArguments(BuiltinArguments):
    pattern: str = Field(
        min_length=1,
        description=(
            "Case-sensitive glob: '*.py' matches at any depth, 'src/*.py' one level, "
            "'src/**/*.py' recursively. Use / separators; braces such as '*.{ts,tsx}' or "
            "'{src,test}/**/*.ts' expand alternatives."
        ),
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Root directory, absolute or workspace-relative. Defaults to the workspace or, "
            "if unset, the current working directory."
        ),
    )
    include_ignored: bool = Field(
        default=False,
        description=(
            "Bypass .gitignore, .ignore, and .rgignore when ignored files are needed. "
            "Default false; other exclusions still apply."
        ),
    )


def builtin_glob_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name="Glob",
            description=GLOB_DESCRIPTION,
            parameters_schema=_GlobArguments.model_json_schema(),
            permission_mode="auto",
            effect="read_only",
        ),
        handler=_glob,
    )


def _glob(
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult:
    arguments = _GlobArguments.model_validate(invocation.arguments)
    pattern = arguments.pattern
    include_ignored = arguments.include_ignored
    root = _directory_arg(arguments.path, context)
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


def _directory_arg(raw: str | None, context: ToolExecutionContext) -> Path:
    if raw is None:
        resolved = context.resolve_working_directory(None)
        root = Path(resolved) if resolved is not None else Path.cwd()
    else:
        root = Path(context.resolve_path(raw, read_only=True))
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
