from typing import Literal

MessageRole = Literal["system", "user", "assistant", "tool_result", "harness"]
RunMessageKind = Literal["input", "assistant_response", "tool_result"]


def run_message_kind(role: MessageRole, *, supplied: bool) -> RunMessageKind:
    """An accepted input or context delivery can present any native role."""
    if supplied:
        return "input"
    if role == "assistant":
        return "assistant_response"
    if role == "tool_result":
        return "tool_result"
    return "input"
