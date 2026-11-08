"""Native Grep arguments, process execution, and result presentation."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

from ouro.core.base import utc_now
from ouro.tools.base import ProcessResult, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin.common import BuiltinArguments, _json_integer, _tool_result
from ouro.tools.builtin.search.files import (
    _MAX_SEARCHABLE_FILE_BYTES,
    DirectoryExclusions,
    _path_arg,
    is_sensitive_file,
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
