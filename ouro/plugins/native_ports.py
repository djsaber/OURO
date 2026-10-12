from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict

from ouro.core.model_generation import ModelGenerationPort, ModelGenerationRequest, ModelTextDelta
from ouro.plugins.catalog import GenerationGate
from ouro.plugins.invocation import current_invocation
from ouro.sdk.plugins.models import ModelOutput, ModelRequest, ModelResult, ModelUsage
from ouro.sdk.plugins.run_drivers import (
    ModelResponse,
    RunDriverContext,
    RunInput,
    RunToolCall,
    ToolBatchResult,
)
from ouro.sdk.plugins.state import JsonValue


class PluginNativePorts:
    """Bind native operations to the plugin's permissions and invocation lifetime."""

    def __init__(
        self,
        gate: GenerationGate,
        permissions: frozenset[str],
        *,
        plugin_id: str,
        generation_service: ModelGenerationPort | None = None,
    ) -> None:
        self.gate = gate
        self.permissions = permissions
        self.plugin_id = plugin_id
        self.generation_service = generation_service

    async def generate(
        self,
        request: ModelRequest,
        *,
        on_output: Callable[[ModelOutput], Awaitable[None]] | None = None,
    ) -> ModelResult:
        if "models.request" not in self.permissions:
            raise PermissionError("Independent model generation requires models.request")
        invocation = current_invocation.get()
        if invocation is not None:
            invocation.require_active()
            if not invocation.writable:
                raise PermissionError("A read-only callback cannot generate model output")
            if invocation.native_run is not None or invocation.run_id is not None:
                raise RuntimeError("An active Run must use its native models.request port")
        if self.generation_service is None:
            raise RuntimeError("Independent model generation is unavailable")
        validated = ModelGenerationRequest.model_validate(asdict(request))

        async def output(delta: ModelTextDelta) -> None:
            assert on_output is not None
            await on_output(ModelOutput(delta.sequence, delta.kind, delta.text))

        async with self.gate.invocation():
            result = await self.generation_service.generate(
                validated,
                plugin_id=self.plugin_id,
                session_id=invocation.session_id if invocation is not None else None,
                workspace=invocation.workspace if invocation is not None else None,
                on_output=output if on_output is not None else None,
            )
        return ModelResult(
            result.request_id,
            result.model,
            result.text,
            result.reasoning,
            ModelUsage(**result.usage.model_dump()) if result.usage is not None else None,
        )

    @asynccontextmanager
    async def _invocation(self) -> AsyncIterator[RunDriverContext]:
        invocation = current_invocation.get()
        if invocation is None or invocation.native_run is None:
            raise RuntimeError("native Run ports require a live parent Run invocation")
        invocation.require_active()
        if not invocation.writable:
            raise PermissionError("a read-only callback cannot execute native Run operations")
        if "sessions.write" not in self.permissions:
            raise PermissionError("native Run operations require sessions.write")
        async with self.gate.invocation():
            yield invocation.native_run

    async def request(
        self, *, prompt: str = "", tools: tuple[str, ...] | None = None
    ) -> ModelResponse:
        async with self._invocation() as context:
            return await context.models.request(prompt=prompt, tools=tools)

    async def consume(self) -> tuple[RunInput, ...]:
        async with self._invocation() as context:
            return await context.inputs.consume()

    async def execute(
        self, calls: tuple[RunToolCall, ...], *, checkpoint: JsonValue
    ) -> ToolBatchResult:
        async with self._invocation() as context:
            return await context.tools.execute(calls, checkpoint=checkpoint)

    async def execute_model_response(
        self, response_id: str, *, checkpoint: JsonValue
    ) -> ToolBatchResult:
        async with self._invocation() as context:
            return await context.tools.execute_model_response(response_id, checkpoint=checkpoint)
