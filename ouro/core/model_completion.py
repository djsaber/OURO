"""Decide whether a terminated response can advance the agent's model/tool loop."""

from dataclasses import dataclass

from ouro.core.provider import ProviderCompletion


@dataclass(frozen=True)
class ModelCompletionFailure:
    code: str
    message: str


def model_completion_failure(
    completion: ProviderCompletion,
    *,
    has_text: bool,
    has_tool_calls: bool,
    has_reasoning: bool = False,
) -> ModelCompletionFailure | None:
    """Termination takes precedence over content, including parseable tool fragments.

    An unspecified reason is allowed for providers without termination metadata;
    an explicitly unsupported reason cannot establish a successful completion.
    """
    if completion.finish_reason == "length":
        return ModelCompletionFailure(
            "model_output_limit", "model response reached the output limit before completion"
        )
    if completion.finish_reason == "content_filter":
        return ModelCompletionFailure(
            "model_content_filtered", "model response was blocked or refused by the provider"
        )
    if completion.finish_reason == "other":
        return ModelCompletionFailure(
            "model_incomplete", "model response ended with an unsupported finish reason"
        )
    if completion.finish_reason == "tool_call" and not has_tool_calls:
        return ModelCompletionFailure(
            "model_missing_tool_call", "provider indicated a tool call but returned no tool call"
        )
    if has_text or has_tool_calls:
        return None
    if has_reasoning or completion.has_reasoning:
        return ModelCompletionFailure(
            "model_reasoning_only", "model returned reasoning without a final answer or tool call"
        )
    return ModelCompletionFailure(
        "model_empty_response", "provider returned an empty assistant message"
    )
