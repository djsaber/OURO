"""Glob tool, shared result ordering, and Python and ripgrep file discovery."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from heapq import heappush_max, heappushpop_max
from pathlib import Path
from typing import Literal

from pydantic import Field

from ouro.tools.base import ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin.common import BuiltinArguments, _tool_result
from ouro.tools.builtin.search.files import (
    FileEntry,
    _matches_any,
    _translate,
    is_sensitive_file,
    iter_files,
)

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
    return _render_glob(invocation, root, pattern, result)


def _render_glob(
    invocation: ToolInvocation,
    root: Path,
    pattern: str,
    result: GlobResult,
) -> ToolResult:
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
        root = Path(context.resolve_path(raw))
    if not root.is_dir():
        raise ValueError(f"search root is not a directory: {root}")
    return root


@dataclass(frozen=True)
class GlobResult:
    matches: tuple[FileEntry, ...]  # most recently modified first, capped at limit
    total: int


@dataclass
class _GlobMatches:
    """Count every match while retaining only the newest files and path tie-breaks."""

    limit: int
    total: int = 0
    heap: list[tuple[float, str, FileEntry]] = field(default_factory=list)

    def add(self, entry: FileEntry) -> None:
        self.total += 1
        if self.limit <= 0:
            return
        ranked = (-entry.mtime, entry.relative, entry)
        if len(self.heap) < self.limit:
            heappush_max(self.heap, ranked)
        else:
            heappushpop_max(self.heap, ranked)

    def result(self) -> GlobResult:
        return GlobResult(tuple(item[2] for item in sorted(self.heap)), self.total)


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
    matches = _GlobMatches(limit)
    for entry in iter_files(
        root,
        include_ignored=include_ignored,
        check_cancelled=check_cancelled,
        file_filter=lambda relative: _matches_any(matchers, relative),
    ):
        matches.add(entry)
    return matches.result()


async def _native_glob(
    executable: str,
    invocation: ToolInvocation,
    context: ToolExecutionContext,
    arguments: _GlobArguments,
    root: Path,
) -> ToolResult:
    matchers = compile_path_glob(arguments.pattern)
    matches = _GlobMatches(_GLOB_MAX_RESULTS)
    pending = b""

    async def collect(stream: Literal["stdout", "stderr"], chunk: bytes) -> None:
        nonlocal pending
        if stream != "stdout":
            return
        # NUL framing handles newlines in filenames and arbitrary pipe chunk boundaries.
        *paths, pending = (pending + chunk).split(b"\0")
        for raw in paths:
            context.raise_if_aborted()
            relative = os.fsdecode(raw).removeprefix("./")
            if not _matches_any(matchers, relative):
                continue
            path = root / relative
            if is_sensitive_file(path.name):
                continue
            try:
                info = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                matches.add(FileEntry(path, relative, info.st_mtime))

    argv = [
        executable,
        "--files",
        "--null",
        "--hidden",
        "--no-follow",
        "--no-config",
        "--color=never",
        "--path-separator",
        "/",
        "--no-require-git",
        "--no-ignore-parent",
        "--no-ignore-global",
        "--no-ignore-exclude",
    ]
    # Keep user glob matching in OURO: rg include globs override ignore rules and
    # use different anchoring. Only unconditional directory exclusions go to rg.
    for name in (".git", ".hg", ".svn"):
        argv += ["--glob", f"!**/{name}/**"]
    if arguments.include_ignored:
        argv.append("--no-ignore")
    result = await context.run_process([*argv, "--", "."], cwd=root, on_output=collect)
    if result.timed_out or result.returncode not in (0, 1):
        message = "Glob search timed out." if result.timed_out else "Glob search failed."
        return _tool_result(
            invocation,
            content=f"{message}\n{result.stderr}".rstrip(),
            is_error=True,
        )
    return _render_glob(invocation, root, arguments.pattern, matches.result())
