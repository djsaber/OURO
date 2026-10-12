from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ouro.application.models import RuntimeAdapterCatalog, model_route_key
from ouro.application.usage import UsageService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextBuilder
from ouro.core.messages import ModelMessage
from ouro.core.model_execution import ModelExecutionEvent, execute_model_request
from ouro.core.model_generation import ModelGenerationRequest, ModelGenerationResult, ModelTextDelta
from ouro.core.reasoning import ReasoningRequest
from ouro.core.request_context import ProviderRequestContext

_LOG = logging.getLogger(__name__)


class ModelGenerationService:
    """One independent provider request using configured adapters and the native ledger."""

    def __init__(self, models: RuntimeAdapterCatalog, usage: UsageService) -> None:
        self.models = models
        self.usage = usage

    def _adapter(self, request: ModelGenerationRequest) -> tuple[str, RuntimeAdapter]:
        route = (
            self.models.configured_default_model().key
            if request.model == "default"
            else request.model
        )
        adapter = self.models.adapter_for(route)
        reasoning = (
            adapter.model.reasoning_request
            if request.reasoning == ReasoningRequest()
            else request.reasoning
        )
        adapter = adapter.with_reasoning(reasoning)
        if request.max_output_tokens is not None:
            if request.max_output_tokens > adapter.model.max_output_tokens:
                raise ValueError("Requested output exceeds the configured model limit")
            adapter = RuntimeAdapter(
                adapter.provider,
                adapter.model.model_copy(update={"max_output_tokens": request.max_output_tokens}),
            )
        return model_route_key(adapter.model), adapter

    async def generate(
        self,
        request: ModelGenerationRequest,
        *,
        plugin_id: str,
        session_id: str | None,
        workspace: str | None,
        on_output: Callable[[ModelTextDelta], Awaitable[None]] | None = None,
    ) -> ModelGenerationResult:
        route, adapter = await run_blocking(self._adapter, request)
        context = ContextBuilder().build(
            messages=[
                ModelMessage.text(role=item.role, text=item.text) for item in request.messages
            ],
            tools=[],
            system_prompt=request.system_prompt,
            context_window_tokens=adapter.model.context_window,
            reserved_output_tokens=adapter.model.max_output_tokens,
        )
        if context.metrics.over_budget:
            raise ValueError("Independent model input exceeds the configured context budget")
        request_id = new_id("mreq")
        sequence = 0

        async def observe(progress: ModelExecutionEvent) -> None:
            nonlocal sequence
            event = progress.event
            if event.type == "done" and event.usage is not None:
                # Settle known usage before publishing the result, including cancellation races.
                await run_blocking(
                    self.usage.record_plugin_usage,
                    plugin_id=plugin_id,
                    request_id=request_id,
                    usage=event.usage,
                    model=adapter.model,
                    session_id=session_id,
                    workspace=workspace,
                )
            if on_output is not None:
                text = (
                    event.text
                    if event.type == "text_delta"
                    else event.reasoning_content
                    if event.type == "reasoning_delta"
                    else ""
                )
                if text:
                    sequence += 1
                    await on_output(
                        ModelTextDelta(
                            sequence, "text" if event.type == "text_delta" else "reasoning", text
                        )
                    )

        values = {"request.purpose": "plugin", "plugin.id": plugin_id, "request.id": request_id}
        if session_id is not None:
            values["session.id"] = session_id
        _LOG.debug("Plugin model request started: %s %s %s", plugin_id, request_id, route)
        try:
            async with asyncio.timeout(request.timeout_seconds):
                result = await execute_model_request(
                    adapter,
                    context,
                    request_context=ProviderRequestContext(values),
                    signal=AbortSignal(),
                    on_event=observe,
                )
            if result.error is not None:
                raise RuntimeError(f"Model request {request_id} failed: {result.error}")
            if result.tool_calls:
                raise ValueError(f"Model request {request_id} returned unrequested tools")
            return ModelGenerationResult(
                request_id, route, result.text, result.reasoning, result.usage
            )
        finally:
            _LOG.debug("Plugin model request settled: %s %s", plugin_id, request_id)
