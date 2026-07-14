from __future__ import annotations

from pathlib import Path

from ouro.core.base import utc_now
from ouro.tools.base import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolResult,
)


def builtin_tools() -> list[Tool]:
    return [
        Tool(
            definition=ToolDefinition(
                name="Read",
                description="Read a text file or list a directory.",
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string", "minLength": 1}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                permission_mode="auto",
            ),
            handler=_read,
        ),
        Tool(
            definition=ToolDefinition(
                name="Write",
                description="Create or overwrite a text file.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                permission_mode="approval",
            ),
            handler=_write,
        ),
        Tool(
            definition=ToolDefinition(
                name="Edit",
                description="Replace exact text in a text file.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "old": {"type": "string", "minLength": 1},
                        "new": {"type": "string"},
                    },
                    "required": ["path", "old", "new"],
                    "additionalProperties": False,
                },
                permission_mode="approval",
            ),
            handler=_edit,
        ),
        Tool(
            definition=ToolDefinition(
                name="Bash",
                description="Run a local shell command.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "minLength": 1},
                        "cwd": {"type": "string", "minLength": 1},
                        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                permission_mode="approval",
            ),
            handler=_bash,
        ),
        Tool(
            definition=ToolDefinition(
                name="AskUser",
                description="Ask the user for missing information.",
                parameters_schema={
                    "type": "object",
                    "properties": {"question": {"type": "string", "minLength": 1}},
                    "required": ["question"],
                    "additionalProperties": False,
                },
                permission_mode="auto",
            ),
            handler=_ask_user,
        ),
    ]


def _path_arg(invocation: ToolInvocation, context: ToolExecutionContext) -> Path:
    path = invocation.arguments.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("tool argument 'path' must be a non-empty string")
    return context.resolve_path(path)


def _text_arg(invocation: ToolInvocation, name: str) -> str:
    value = invocation.arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"tool argument '{name}' must be a string")
    return value


def _read(
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext,
) -> ToolResult:
    path = _path_arg(invocation, context)
    if path.is_dir():
        content = "\n".join(sorted(child.name for child in path.iterdir()))
        structured: dict[str, object] = {"kind": "directory", "path": str(path)}
    else:
        content = path.read_text(encoding="utf-8")
        structured = {"kind": "file", "path": str(path)}
    content, truncated = context.limit_output(content)
    structured["truncated"] = truncated
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=content,
        structured=structured,
        ended_at=utc_now(),
    )


def _write(
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext,
) -> ToolResult:
    path = _path_arg(invocation, context)
    content = _text_arg(invocation, "content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=f"Wrote {path}",
        structured={"path": str(path), "bytes": len(content.encode("utf-8"))},
        ended_at=utc_now(),
    )


def _edit(
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext,
) -> ToolResult:
    path = _path_arg(invocation, context)
    old = _text_arg(invocation, "old")
    new = _text_arg(invocation, "new")
    text = path.read_text(encoding="utf-8")
    if old not in text:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            is_error=True,
            content="Old text was not found.",
            structured={"path": str(path), "changed": False},
            ended_at=utc_now(),
        )
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=f"Edited {path}",
        structured={"path": str(path), "changed": True},
        ended_at=utc_now(),
    )


async def _bash(
    invocation: ToolInvocation,
    *,
    context: ToolExecutionContext | None = None,
) -> ToolResult:
    command = _text_arg(invocation, "command")
    cwd_arg = invocation.arguments.get("cwd")
    requested_cwd = str(cwd_arg) if isinstance(cwd_arg, str) and cwd_arg else None
    timeout_raw = invocation.arguments.get("timeout_seconds", 30)
    timeout_seconds = float(timeout_raw) if isinstance(timeout_raw, int | float) else 30.0
    context = context or ToolExecutionContext()
    cwd = context.resolve_working_directory(requested_cwd)
    process_result = await context.run_shell(
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )

    if process_result.timed_out:
        content = "Command timed out."
    elif process_result.aborted:
        content = "Command aborted."
    else:
        content = ""
    stdout_text, stdout_truncated = context.limit_output(process_result.stdout)
    stderr_text, stderr_truncated = context.limit_output(process_result.stderr)
    if not content:
        content = stdout_text if stdout_text else stderr_text
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        is_error=process_result.aborted
        or process_result.timed_out
        or (process_result.returncode not in (0, None)),
        content=content,
        structured={
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "timed_out": process_result.timed_out,
            "aborted": process_result.aborted,
        },
        exit_code=process_result.returncode,
        ended_at=utc_now(),
    )


def _ask_user(invocation: ToolInvocation) -> ToolResult:
    question = _text_arg(invocation, "question")
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=question,
        structured={"question": question},
        ended_at=utc_now(),
    )
