"""Glob and Grep tools with ripgrep selection and Python search backends."""

from __future__ import annotations

import os
import re
import shutil
import stat
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from heapq import heappush_max, heappushpop_max
from itertools import islice
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pathspec import GitIgnoreSpec
from pydantic import BeforeValidator, Field
from wcmatch import glob as wcglob

from ouro.core.base import utc_now
from ouro.tools.base import (
    ProcessResult,
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolResult,
)
from ouro.tools.builtin.common import BuiltinArguments, JsonInteger, _json_integer, _tool_result

_GLOB_MAX_RESULTS = 100


GLOB_DESCRIPTION = """\
Find files by name or path pattern. Use Grep for content searches and Read for known files.
Returns up to 100 paths relative to the search root, newest modified first; never lists
directories. To locate a directory, match files inside it (e.g. '**/fixtures/**').
Narrow truncated searches; use specific subpaths in dependency or build directories.
Includes hidden files; always skips .env, .env.*, version-control metadata, and symlinks."""


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


# CASE pins case-sensitive matching on every platform; DOTGLOB includes dotfiles.
_GLOB_FLAGS = wcglob.GLOBSTAR | wcglob.BRACE | wcglob.DOTGLOB | wcglob.CASE


# File kind takes precedence over directory depth (ripgrep semantics).
_IGNORE_FILENAMES = (".gitignore", ".ignore", ".rgignore")

# VCS metadata is never searched, even when ignore rules are disabled.
_VCS_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn"})


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


class _RipgrepArguments(BuiltinArguments):
    pattern: str = Field(
        min_length=1,
        description=(
            "Ripgrep regex (Unicode supported; no lookaround or backreferences), "
            "or literal text with fixed_strings."
        ),
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "File or directory, absolute or workspace-relative. Defaults to the workspace; "
            "required without a workspace. An explicit file bypasses ignore and file filters."
        ),
    )
    glob: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Ripgrep glob relative to path, e.g. 'src/**/*.py', '*.{ts,tsx}', '!*.min.js'. "
            "An include glob overrides ignore rules and type filtering for matching files."
        ),
    )
    output_mode: Literal["content", "files_with_matches", "count_matches"] = Field(
        default="files_with_matches",
        description=(
            "files_with_matches: paths only; content: path:line:text with optional context; "
            "count_matches: path:matching-line-count. Output follows native search order."
        ),
    )
    context: Annotated[int, Field(ge=0), BeforeValidator(_json_integer)] | None = Field(
        default=None,
        alias="-C",
        description="Context lines on both sides of matches; content mode only. Default 0.",
    )
    ignore_case: bool = Field(default=False, alias="-i", description="Ignore case in matching.")
    fixed_strings: bool = Field(
        default=False,
        description="Treat pattern as literal text instead of a regular expression.",
    )
    include_ignored: bool = Field(
        default=False,
        description="Bypass .gitignore, .ignore and .rgignore; other exclusions still apply.",
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Time limit for the entire search, in seconds. Default 30.",
    )
    file_type: str | None = Field(
        default=None,
        alias="type",
        min_length=1,
        description="Ripgrep's built-in file type, e.g. py, rust, js, ts, md.",
    )
    multiline: bool = Field(
        default=False,
        description=(
            "Allow matches across lines and let . match newlines. "
            "In this mode count_matches counts regex matches, not matching lines."
        ),
    )
    exclude_dirs: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list,
        description=(
            "Directories to prune even with include_ignored: a literal name such as "
            "'node_modules' at any depth, or a root-relative path such as 'vendor/generated'. "
            "Case-sensitive; no glob syntax. Applies to directory searches only."
        ),
    )


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
    exclude_dirs: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list,
        description=(
            "Directories to prune even with include_ignored: a literal name such as "
            "'node_modules' at any depth, or a root-relative path such as 'vendor/generated'. "
            "Case-sensitive; no glob syntax. Applies to directory searches only."
        ),
    )


def detect_ripgrep(
    *,
    resolve_executable: Callable[[str], str | None] | None = None,
) -> str | None:
    return (resolve_executable or shutil.which)("rg")


def builtin_search_tools() -> list[Tool]:
    executable = detect_ripgrep()
    return [builtin_glob_tool(executable), builtin_grep_tool(executable)]


def builtin_glob_tool(executable: str | None) -> Tool:
    async def execute(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
        arguments = _GlobArguments.model_validate(invocation.arguments)
        root = _directory_arg(arguments.path, context)
        assert executable is not None
        return await _native_glob(executable, invocation, context, arguments, root)

    return Tool(
        definition=ToolDefinition(
            name="Glob",
            description=GLOB_DESCRIPTION,
            parameters_schema=_GlobArguments.model_json_schema(),
            permission_mode="auto",
            effect="read_only",
        ),
        handler=execute if executable is not None else _glob,
    )


def builtin_grep_tool(executable: str | None) -> Tool:
    if executable is not None:
        return builtin_ripgrep_tool(executable)
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


def builtin_ripgrep_tool(executable: str) -> Tool:
    async def execute(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
        arguments = _RipgrepArguments.model_validate(invocation.arguments)
        return await _execute_ripgrep(executable, arguments, invocation, context)

    return Tool(
        definition=ToolDefinition(
            name="Grep",
            description=(
                "Search file contents with ripgrep regex, glob and file-type syntax, including "
                "multiline search. Use Glob for filenames and Read for known file ranges. "
                "Includes hidden files; excludes .env, .env.* and files over 32 MiB; uses "
                "native binary detection. Directory searches skip symlinks and version-control "
                "metadata. Use exclude_dirs to prune unwanted subtrees. Output is bounded; "
                "narrow the path or pattern if truncated. "
                "Native diagnostics are returned as errors."
            ),
            parameters_schema=_RipgrepArguments.model_json_schema(),
            permission_mode="auto",
            effect="read_only",
            metadata={"grep_backend": "ripgrep"},
        ),
        handler=execute,
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


def _ripgrep_arguments(
    executable: str,
    arguments: _RipgrepArguments,
    target: Path,
    exclusions: DirectoryExclusions,
) -> list[str]:
    argv = [
        executable,
        "--no-config",
        "--color=never",
        "--no-heading",
        "--with-filename",
        "--hidden",
        "--no-follow",
        "--max-filesize",
        str(_MAX_SEARCHABLE_FILE_BYTES),
        "--no-require-git",
        "--no-ignore-parent",
        "--no-ignore-global",
        "--no-ignore-exclude",
    ]
    if arguments.include_ignored:
        argv.append("--no-ignore")
    if arguments.fixed_strings:
        argv.append("--fixed-strings")
    if arguments.ignore_case:
        argv.append("-i")
    if arguments.output_mode == "files_with_matches":
        argv.append("-l")
    elif arguments.output_mode == "count_matches":
        argv.append("-c")
    else:
        argv.append("-n")
        if arguments.context:
            argv += ["-C", str(arguments.context)]
    if arguments.multiline:
        argv += ["--multiline", "--multiline-dotall"]
    if target.is_dir():
        if arguments.glob is not None:
            argv += ["--glob", arguments.glob]
        if arguments.file_type is not None:
            argv += ["--type", arguments.file_type]
        # Unconditional exclusions follow include globs so they cannot be overridden.
        for excluded in (".env", ".env.*", ".git", ".hg", ".svn"):
            argv += ["--glob", f"!{excluded}"]
        for values, prefix in ((exclusions.names, "**/"), (exclusions.paths, "/")):
            for value in sorted(values):
                escaped = "".join("\\" + char if char in "\\*?[]{}" else char for char in value)
                argv += ["--glob", f"!{prefix}{escaped}/**"]
    return [*argv, "-e", arguments.pattern, "--", "." if target.is_dir() else f"./{target.name}"]


async def _execute_ripgrep(
    executable: str,
    arguments: _RipgrepArguments,
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult:
    context.raise_if_aborted()
    exclusions = DirectoryExclusions.parse(arguments.exclude_dirs)
    target = _path_arg(arguments.path, context)
    if not target.is_file() and not target.is_dir():
        raise ValueError(f"search path is not a file or directory: {target}")
    if target.is_file() and is_sensitive_file(target.name):
        return _tool_result(
            invocation,
            content="Grep does not search sensitive files such as .env.",
            is_error=True,
        )
    if target.is_file() and target.stat().st_size > _MAX_SEARCHABLE_FILE_BYTES:
        result = ProcessResult("", "", 1)
    else:
        result = await context.run_process(
            _ripgrep_arguments(executable, arguments, target, exclusions),
            cwd=target if target.is_dir() else target.parent,
            timeout_seconds=arguments.timeout_seconds,
        )
    is_error = result.timed_out or result.returncode not in (0, 1)
    content = result.stdout.rstrip("\n")
    if not content and not is_error:
        content = "No matches found."
    if result.stderr:
        content += f"\nstderr:\n{result.stderr}"
    if result.timed_out:
        content += "\nSearch timed out."
    if result.stdout_truncated or result.stderr_truncated:
        content += "\nResults truncated; narrow the path or pattern."
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=content,
        is_error=is_error,
        structured={
            "kind": "grep",
            "backend": "ripgrep",
            "mode": arguments.output_mode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "timed_out": result.timed_out,
        },
        exit_code=result.returncode,
        ended_at=utc_now(),
    )


def _grep(
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult:
    arguments = _GrepArguments.model_validate(invocation.arguments)
    exclude_dirs = DirectoryExclusions.parse(arguments.exclude_dirs)
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
            stop_after_first=output_mode == "files_with_matches",
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
            stop_after_first=output_mode == "files_with_matches",
            exclude_dirs=exclude_dirs,
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
    total: int  # matched lines, or matching files for existence-only searches
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
    stop_after_first: bool = False,
    exclude_dirs: DirectoryExclusions | None = None,
) -> GrepResult:
    regex = _compile_search_regex(pattern, ignore_case=ignore_case, multiline=multiline)
    root_prefix = root.as_posix().rstrip("/") + "/"

    def file_filter(relative: str) -> bool:
        name = relative.rsplit("/", 1)[-1]
        return (
            name_filter is None
            or _matches_any(
                name_filter.matchers,
                root_prefix + relative if name_filter.anchored else name,
            )
        ) and (type_filter is None or _matches_any(type_filter, name))

    entries = iter_files(
        root,
        include_ignored=include_ignored,
        check_cancelled=check_cancelled,
        file_filter=file_filter if name_filter is not None or type_filter is not None else None,
        exclude_dirs=exclude_dirs,
    )
    return _search_entries(
        entries,
        regex,
        multiline=multiline,
        context_before=context_before,
        context_after=context_after,
        collect_content=collect_content,
        check_cancelled=check_cancelled,
        stop_after_first=stop_after_first,
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
    stop_after_first: bool,
) -> GrepResult:
    matches: list[MatchedLine] = []
    file_counts: list[FileMatchCount] = []
    total = 0
    literal = _literal_pattern(regex) if not multiline else None
    literal_exists = (
        stop_after_first
        and not collect_content
        and literal is not None
        and literal != ""
        and not any(char in literal for char in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
    )
    for entry in entries:
        if check_cancelled is not None:
            check_cancelled()
        text = _read_searchable_text(entry.path)
        if check_cancelled is not None:
            check_cancelled()
        if text is None:
            continue
        if literal is not None and literal not in text:
            continue
        if literal_exists:
            file_counts.append(FileMatchCount(entry=entry, count=1))
            total += 1
            continue
        lines = text.splitlines()
        numbers = _matched_line_numbers(lines, regex, multiline=multiline, literal=literal)
        count = 0
        if stop_after_first and not collect_content:
            count = int(next(numbers, None) is not None)
        elif collect_content:
            for number in islice(numbers, _MAX_COLLECTED_MATCHES - len(matches)):
                count += 1
                matches.append(
                    MatchedLine(
                        entry=entry,
                        number=number,
                        text=lines[number - 1],
                        before=_context(lines, number, -context_before) if context_before else (),
                        after=_context(lines, number, context_after) if context_after else (),
                    )
                )
            count += sum(1 for _ in numbers)
        elif not multiline:
            count = (
                sum(literal in line for line in lines)
                if literal is not None
                else sum(regex.search(line) is not None for line in lines)
            )
        else:
            count = sum(1 for _ in numbers)
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
    stop_after_first: bool = False,
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
        stop_after_first=stop_after_first,
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
    literal: str | None,
) -> Iterator[int]:
    if not multiline:
        if literal is not None:
            yield from (index + 1 for index, line in enumerate(lines) if literal in line)
        else:
            search = regex.search
            yield from (index + 1 for index, line in enumerate(lines) if search(line) is not None)
        return
    if not lines:
        return
    offsets = _line_start_offsets(lines)
    emitted = 0
    text = "\n".join(lines)
    for hit in regex.finditer(text):
        first = bisect_right(offsets, hit.start())
        last = bisect_right(offsets, max(hit.start(), hit.end() - 1))
        # finditer visits non-overlapping spans in order. Emit their line union
        # incrementally, without a potentially file-sized set and final sort.
        yield from range(max(first, emitted + 1), last + 1)
        emitted = max(emitted, last)


def _literal_pattern(regex: re.Pattern[str]) -> str | None:
    if regex.flags & re.IGNORECASE or any(char in r".\^$*+?{}[]|()" for char in regex.pattern):
        return None
    return regex.pattern


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
            if os.fstat(stream.fileno()).st_size > _MAX_SEARCHABLE_FILE_BYTES:
                return None
            head = stream.read(_BINARY_SNIFF_BYTES)
            if len(head) > _MAX_SEARCHABLE_FILE_BYTES or b"\x00" in head:
                return None
            content = bytearray(head)
            while chunk := stream.read(64 * 1024):
                content.extend(chunk)
                if len(content) > _MAX_SEARCHABLE_FILE_BYTES:
                    return None
        return content.decode("utf-8", errors="replace")
    except OSError:
        return None


@dataclass(frozen=True)
class FileEntry:
    """One searchable file discovered under the search root."""

    path: Path
    relative: str  # POSIX separators, relative to the search root
    mtime: float


@dataclass(frozen=True)
class DirectoryExclusions:
    names: frozenset[str] = frozenset()
    paths: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, values: list[str]) -> DirectoryExclusions:
        names: set[str] = set()
        paths: set[str] = set()
        for value in values:
            path = PurePosixPath(value.replace("\\", "/"))
            if (
                path.is_absolute()
                or PureWindowsPath(value).drive
                or ".." in path.parts
                or not path.parts
            ):
                raise ValueError(
                    "exclude_dirs entries must be directory names or root-relative paths"
                )
            (paths if len(path.parts) > 1 else names).add(path.as_posix())
        return cls(frozenset(names), frozenset(paths))

    def matches(self, name: str, relative: str) -> bool:
        return name in self.names or relative in self.paths


def iter_files(
    root: Path,
    *,
    include_ignored: bool,
    check_cancelled: Callable[[], None] | None = None,
    file_filter: Callable[[str], bool] | None = None,
    exclude_dirs: DirectoryExclusions | None = None,
) -> Iterator[FileEntry]:
    """Yield regular files under ``root`` in a deterministic, pruned walk."""

    rules = IgnoreRules(root, enabled=not include_ignored)
    pending = [(root, "")]
    while pending:
        base, prefix = pending.pop()
        if check_cancelled is not None:
            check_cancelled()
        try:
            # Keep DirEntry's cached type/stat information, and close the directory
            # before yielding so cancellation or a partial consumer cannot leak it.
            with os.scandir(base) as directory:
                entries = sorted(directory, key=lambda entry: entry.name)
        except OSError:
            continue
        ignored = rules.matcher(base)
        directories: list[tuple[Path, str]] = []
        for index, entry in enumerate(entries):
            if check_cancelled is not None and index % 128 == 0:
                check_cancelled()
            try:
                if entry.is_symlink():
                    continue
                name = entry.name
                relative = prefix + name
                if entry.is_dir(follow_symlinks=False):
                    if (
                        not is_vcs_directory(name)
                        and (exclude_dirs is None or not exclude_dirs.matches(name, relative))
                        and (ignored is None or not ignored(name + "/"))
                    ):
                        directories.append((base / name, relative + "/"))
                    continue
                if is_sensitive_file(name) or (
                    file_filter is not None and not file_filter(relative)
                ):
                    continue
                if ignored is not None and ignored(name):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            yield FileEntry(
                path=base / name,
                relative=relative,
                mtime=stat.st_mtime,
            )
        # Match os.walk's files-first, lexicographic depth-first ordering.
        pending.extend(reversed(directories))


def _translate(pattern: str) -> tuple[re.Pattern[str], ...]:
    inclusive, exclusive = wcglob.translate([pattern], flags=_GLOB_FLAGS)
    if exclusive:
        raise ValueError(f"glob pattern must not use negation: {pattern!r}")
    return tuple(re.compile(expression) for expression in inclusive)


def _matches_any(matchers: tuple[re.Pattern[str], ...], value: str) -> bool:
    for matcher in matchers:
        if matcher.match(value) is not None:
            return True
    return False


def is_vcs_directory(name: str) -> bool:
    return name in _VCS_DIRECTORY_NAMES


def is_sensitive_file(name: str) -> bool:
    """Secrets-bearing dotfiles are excluded from every search, unconditionally."""

    return name == ".env" or name.startswith(".env.")


class IgnoreRules:
    """Hierarchical ignore matcher rooted at one search directory.

    Rules are loaded lazily per directory and cached. Higher-priority ignore
    file kinds win; within each kind, deeper directories take precedence.
    """

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self._root = root
        self._enabled = enabled
        self._chains: dict[Path, tuple[tuple[Path, dict[str, GitIgnoreSpec]], ...]] = {}

    def matcher(self, directory: Path) -> Callable[[str], bool] | None:
        """Bind rule prefixes once per directory; inputs are names with / for directories."""
        if not self._enabled:
            return None
        chain = self._chain(directory)
        checks: list[tuple[str, GitIgnoreSpec]] = []
        for filename in reversed(_IGNORE_FILENAMES):
            for base, specs in chain:
                spec = specs.get(filename)
                if spec is not None:
                    relative = directory.relative_to(base).as_posix()
                    checks.append(("" if relative == "." else relative + "/", spec))
        if not checks:
            return None

        def ignored(name: str) -> bool:
            for prefix, spec in checks:
                result = spec.check_file(prefix + name)
                if result.include is not None:
                    return result.include
            return False

        return ignored

    def _chain(self, directory: Path) -> tuple[tuple[Path, dict[str, GitIgnoreSpec]], ...]:
        """Cache only applicable rule sets per directory for this search invocation."""
        missing: list[Path] = []
        current = directory
        while current not in self._chains:
            missing.append(current)
            if current == self._root:
                break
            current = current.parent
        chain = self._chains.get(current, ())
        for base in reversed(missing):
            specs = self._read_specs(base)
            if specs:
                chain = ((base, specs), *chain)
            self._chains[base] = chain
        return chain

    def _read_specs(self, directory: Path) -> dict[str, GitIgnoreSpec]:
        specs: dict[str, GitIgnoreSpec] = {}
        for filename in _IGNORE_FILENAMES:
            candidate = directory / filename
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            specs[filename] = GitIgnoreSpec.from_lines(text.splitlines())
        return specs


__all__ = ["builtin_search_tools"]
