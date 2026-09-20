from typing import Literal

OpenAIResponsesErrorCode = Literal[
    "invalid_output_item",
    "conflicting_output_item",
    "unfinished_output_item",
    "invalid_output",
    "invalid_event",
    "conflicting_delta",
    "invalid_tool_arguments",
    "duplicate_tool_call",
    "unsupported_output_item",
    "invalid_usage",
    "invalid_terminal",
    "duplicate_terminal",
    "output_after_terminal",
    "missing_terminal",
    "response_failed",
    "response_incomplete",
    "invalid_continuation",
    "continuation_scope_mismatch",
    "continuation_route_mismatch",
    "continuation_tool_mismatch",
    "unsupported_input",
    "invalid_tool_schema",
]


class OpenAIResponsesError(ValueError):
    """Fixed protocol facts; raw payloads, arguments and ciphertext never enter diagnostics."""

    def __init__(self, code: OpenAIResponsesErrorCode) -> None:
        self.code = code
        message = "Responses protocol error: " + code
        if code in {"continuation_scope_mismatch", "continuation_route_mismatch"}:
            message += ". Restore the original model route/account or start a new session."
        super().__init__(message)
