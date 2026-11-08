"""Select available search backends and register built-in tools."""

from __future__ import annotations

import shutil
from collections.abc import Callable

from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext, ToolInvocation, ToolResult
from ouro.tools.builtin.search.glob import (
    GLOB_DESCRIPTION,
    _directory_arg,
    _glob,
    _GlobArguments,
    _native_glob,
)
from ouro.tools.builtin.search.grep import GREP_DESCRIPTION, _grep, _GrepArguments
from ouro.tools.builtin.search.ripgrep import _execute_ripgrep, _RipgrepArguments


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
