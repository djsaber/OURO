from __future__ import annotations

from ouro.tools.base import (
    Tool,
    ToolDefinition,
    ToolInputRequest,
    ToolInvocation,
)
from ouro.tools.builtin_files import builtin_file_tools
from ouro.tools.builtin_shell import builtin_command_tool, detect_command_shell


def builtin_tools() -> list[Tool]:
    shell = detect_command_shell()
    return [
        *builtin_file_tools(),
        *([builtin_command_tool(shell)] if shell is not None else []),
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


def _ask_user(invocation: ToolInvocation) -> ToolInputRequest:
    return ToolInputRequest(data=invocation.arguments["question"])
