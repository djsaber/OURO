"""Consume a protocol probe without requiring a full agent answer."""

from collections.abc import AsyncIterator

from ouro.core.model_completion import model_completion_failure
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent


async def check_connection_events(
    events: AsyncIterator[ProviderStreamEvent], *, provider_id: str, model: ModelMetadata
) -> ProviderCheckResult:
    has_text = False
    has_tool_calls = False
    has_reasoning = False
    completion = None
    error = ""
    error_code = None
    try:
        async for event in events:
            if event.type == "error":
                error = event.error or "provider connection check failed"
                error_code = event.error_code
                break
            if event.type == "text_delta":
                has_text = has_text or bool(event.text and event.text.strip())
            elif event.type == "reasoning_delta":
                has_reasoning = has_reasoning or bool(event.reasoning_content)
            elif event.type == "tool_call_delta":
                has_tool_calls = True
            elif event.type == "done":
                completion = event.completion
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            await close()
    if not error:
        if completion is None:
            error = "provider stream ended without a completion event"
        else:
            failure = model_completion_failure(
                completion,
                has_text=has_text,
                has_tool_calls=has_tool_calls,
                has_reasoning=has_reasoning,
            )
            # A small probe budget may end during reasoning. It still demonstrates
            # connectivity; the agent loop independently requires a usable answer.
            if failure is not None and failure.code not in {
                "model_output_limit",
                "model_reasoning_only",
            }:
                error, error_code = failure.message, failure.code
    return ProviderCheckResult(
        provider_id=provider_id,
        model_id=model.model_id,
        ok=not error,
        message=error or "ok",
        error_code=error_code,
        metadata={"endpoint_id": model.endpoint_id} if model.endpoint_id else {},
    )
