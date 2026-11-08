from __future__ import annotations

from ouro.tools.base import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInputRequest,
    ToolInvocation,
)
from ouro.tools.builtin.ask_user_contract import (
    ASK_USER_REQUEST_TYPE,
    ASK_USER_TOOL_NAME,
    AskUserAnswer,
    AskUserArguments,
    AskUserOption,
    AskUserQuestion,
    AskUserRequest,
    parse_builtin_ask_user_request,
)

_ASK_USER_DESCRIPTION = """\
Ask the user 1-3 questions when missing information or preferences materially affect the work
and cannot be inferred or discovered. Provide 2-4 options per question; free-form answers are
always available. Avoid trivial choices you can resolve yourself. Wait for the answers before
continuing dependent work.
"""


def builtin_ask_user_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name=ASK_USER_TOOL_NAME,
            description=_ASK_USER_DESCRIPTION,
            parameters_schema=AskUserArguments.model_json_schema(),
            permission_mode="auto",
            execution_mode="sequential",
            effect="read_only",
            batch_behavior="exclusive",
            kind="user_input",
        ),
        handler=_ask_user,
    )


def _ask_user(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolInputRequest:
    arguments = AskUserArguments.model_validate(invocation.arguments)
    request = AskUserRequest(questions=arguments.questions)
    return ToolInputRequest(data=request.to_json())


__all__ = [
    "ASK_USER_REQUEST_TYPE",
    "ASK_USER_TOOL_NAME",
    "AskUserAnswer",
    "AskUserArguments",
    "AskUserOption",
    "AskUserQuestion",
    "AskUserRequest",
    "builtin_ask_user_tool",
    "parse_builtin_ask_user_request",
]
