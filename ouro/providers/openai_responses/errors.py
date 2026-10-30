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
    "continuation_tool_mismatch",
    "unsupported_input",
    "invalid_tool_schema",
]


class OpenAIResponsesError(ValueError):
    """Fixed protocol facts; raw payloads, arguments and ciphertext never enter diagnostics."""

    def __init__(self, code: OpenAIResponsesErrorCode, *, detail: str | None = None) -> None:
        # Detail contains only local check names and numeric coordinates, never wire values.
        self.code = code
        super().__init__("Responses protocol error: " + code + (f" ({detail})" if detail else ""))
