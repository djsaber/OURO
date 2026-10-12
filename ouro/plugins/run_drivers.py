from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

from ouro.core import run_drivers as core
from ouro.core.plugin_contracts import PluginImplementation
from ouro.plugins.invocation import current_invocation, plugin_invocation
from ouro.plugins.tool_content import decode_worker_content
from ouro.sdk.plugins import run_drivers as sdk


@dataclass(frozen=True)
class RunDriverRegistration:
    driver_id: str
    handler: core.RunDriverHandler
    plugin_id: str = ""
    release_id: str = ""
    owner: str = ""
    contract_hash: str = ""
    execution_contract_hash: str = ""
    implementation: PluginImplementation | None = None
    dependencies: tuple[PluginImplementation, ...] = ()

    @property
    def canonical_id(self) -> str:
        return f"{self.plugin_id}/run_drivers/{self.driver_id}"

    @property
    def binding(self) -> core.RunDriverBinding:
        return core.RunDriverBinding(
            driver_id=self.canonical_id,
            contract_hash=self.contract_hash,
            implementation=self.implementation,
            dependencies=self.dependencies,
        )


class RunDriverRegistry:
    def __init__(self) -> None:
        self._drivers: dict[str, RunDriverRegistration] = {}
        self._frozen = False

    def replace_owner(self, owner: str, registrations: tuple[RunDriverRegistration, ...]) -> None:
        if self._frozen:
            raise RuntimeError("Run driver registry is frozen")
        retained = {key: item for key, item in self._drivers.items() if item.owner != owner}
        for item in registrations:
            if item.canonical_id in retained:
                raise ValueError(f"duplicate Run driver: {item.canonical_id}")
            retained[item.canonical_id] = replace(item, owner=owner)
        self._drivers = retained

    def freeze(self) -> None:
        self._frozen = True

    def get(self, driver_id: str) -> RunDriverRegistration:
        try:
            return self._drivers[driver_id]
        except KeyError as exc:
            raise ValueError(f"Run driver is unavailable: {driver_id}") from exc


def _result(value: core.ToolResult) -> sdk.NativeToolResult:
    return sdk.NativeToolResult(
        value.tool_call_id,
        value.tool_name,
        decode_worker_content(value.content),
        value.is_error,
        value.structured,
        tuple(value.resource_refs),
    )


class SDKRunDriverContext:
    """Expose invocation-scoped ports without exposing mutable host records."""

    def __init__(self, context: core.RunDriverContext) -> None:
        self._context = context

    @property
    def session_id(self) -> str:
        return self._context.session_id

    @property
    def run_id(self) -> str:
        return self._context.run_id

    @property
    def is_aborted(self) -> bool:
        return self._context.is_aborted

    @property
    def models(self) -> sdk.RunModels:
        return self

    @property
    def tools(self) -> sdk.RunTools:
        return self

    @property
    def agents(self) -> sdk.RunAgents:
        return sdk.RunAgents(self)

    @property
    def inputs(self) -> sdk.RunInputs:
        return self

    async def consume(self) -> tuple[sdk.RunInput, ...]:
        self._require_invocation()
        messages = await self._context.consume_inputs()
        return tuple(
            sdk.RunInput(
                message.message_id,
                message.role,
                tuple(block.model_dump(mode="json") for block in message.content),
                message.scaffold,
                message.semantic_tag,
                message.model_dump(mode="json")["metadata"],
                tuple(message.resource_refs),
            )
            for message in messages
        )

    def _require_invocation(self) -> None:
        invocation = current_invocation.get()
        if invocation is None:
            raise RuntimeError("native Run ports require a live parent Run invocation")
        invocation.require_active()
        if invocation.native_run is not self:
            raise RuntimeError("native Run port belongs to another invocation")
        if not invocation.writable:
            raise PermissionError("a read-only callback cannot execute native Run operations")

    async def request(
        self, *, prompt: str = "", tools: tuple[str, ...] | None = None
    ) -> sdk.ModelResponse:
        self._require_invocation()
        value = await self._context.request_model(prompt=prompt, tools=tools)
        return sdk.ModelResponse(
            value.response_id,
            value.text,
            value.reasoning,
            tuple(
                sdk.ModelToolCall(item.id, item.name, item.arguments) for item in value.tool_calls
            ),
        )

    async def execute(
        self, calls: tuple[sdk.RunToolCall, ...], *, checkpoint: sdk.JsonValue
    ) -> sdk.ToolBatchResult:
        self._require_invocation()
        value = await self._context.execute_tools(
            tuple(
                core.ToolCallRequest(name=item.name, arguments=item.arguments, source_index=index)
                for index, item in enumerate(calls)
            ),
            checkpoint=checkpoint,
        )
        return sdk.ToolBatchResult(value.suspended, tuple(_result(item) for item in value.results))

    async def execute_model_response(
        self, response_id: str, *, checkpoint: sdk.JsonValue
    ) -> sdk.ToolBatchResult:
        self._require_invocation()
        value = await self._context.execute_model_tools(response_id, checkpoint=checkpoint)
        return sdk.ToolBatchResult(value.suspended, tuple(_result(item) for item in value.results))


@contextmanager
def run_driver_invocation(
    owner_id: str, context: core.RunDriverContext
) -> Iterator[SDKRunDriverContext]:
    """Establish the scope of a Host-accepted Run independently of its initiating callback."""
    native_run = SDKRunDriverContext(context)
    with plugin_invocation(
        owner_id,
        writable=True,
        session_id=context.session_id,
        run_id=context.run_id,
        workspace=context.workspace,
        workspace_only=context.workspace_only,
        native_run=native_run,
    ):
        yield native_run


def adapt_run_driver(
    contribution: sdk.RunDriverContribution, *, owner_id: str
) -> RunDriverRegistration:
    async def handler(
        context: core.RunDriverContext, resume: core.DriverResume | None
    ) -> core.DriverFinished | core.DriverSuspended:
        with run_driver_invocation(owner_id, context) as native_run:
            outcome = await contribution.handler(
                native_run,
                sdk.DriverResume(resume.checkpoint, tuple(_result(item) for item in resume.results))
                if resume is not None
                else None,
            )
        if isinstance(outcome, sdk.DriverFinished):
            return core.DriverFinished(outcome.text, response_id=outcome.response_id)
        if isinstance(outcome, sdk.DriverSuspended):
            return core.DriverSuspended()
        raise TypeError("Run driver must return DriverFinished or DriverSuspended")

    return RunDriverRegistration(contribution.id, handler)
