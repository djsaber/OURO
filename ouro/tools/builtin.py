from __future__ import annotations

from ouro.core.base import utc_now
from ouro.tools.base import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInputRequest,
    ToolInvocation,
    ToolResult,
)
from ouro.tools.builtin_files import builtin_file_tools


def builtin_tools() -> list[Tool]:
    return [
        *builtin_file_tools(),
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
                effect="state_changing",
            ),
            handler=_bash,
        ),
        Tool(
            definition=ToolDefinition(
                name="AskUser",
                description="Ask the user for missing information.",
                parameters_schema={
                    "type": "object",
                    "properties": {"question": {}},
                    "required": ["question"],
                    "additionalProperties": False,
                },
                permission_mode="auto",
                execution_mode="sequential",
                effect="read_only",
                batch_behavior="exclusive",
                kind="user_input",
            ),
            handler=_ask_user,
        ),
    ]


def _text_arg(invocation: ToolInvocation, name: str) -> str:
    value = invocation.arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"tool argument '{name}' must be a string")
    return value


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


def _ask_user(invocation: ToolInvocation) -> ToolInputRequest:
    return ToolInputRequest(data=invocation.arguments["question"])
