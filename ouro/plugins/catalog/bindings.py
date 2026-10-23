"""Catalog wrappers that bind contributions to their generation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ouro.core.callbacks import invoke_owned_callback
from ouro.core.compaction import CompactionArtifact, CompactionRequest, CompactionStrategy
from ouro.core.context_providers import (
    ContextProviderCall,
    ContextProviderRegistration,
    ContextProviderResult,
)
from ouro.core.continuations import ContinuationDecision, ContinuationPolicyRegistration
from ouro.core.observers import Observation, ObserverRegistration
from ouro.core.prompt_policies import (
    PromptPolicyCall,
    PromptPolicyDecision,
    PromptPolicyRegistration,
)
from ouro.core.run_drivers import DriverFinished, DriverResume, DriverSuspended, RunDriverContext
from ouro.core.state import RunOutcome
from ouro.core.tool_policies import ToolPolicyCall, ToolPolicyDecision, ToolPolicyRegistration
from ouro.core.turn_policies import (
    TurnCompletionCandidate,
    TurnPolicyDecision,
    TurnPolicyRegistration,
)
from ouro.plugins.catalog.gates import GenerationGate
from ouro.plugins.contributions.models import PreparedContributionSet
from ouro.plugins.providers import ProviderRegistration
from ouro.plugins.registry import PluginCommand, PluginCommandRequest, PluginCommandResult
from ouro.plugins.run_drivers import RunDriverRegistration
from ouro.plugins.session_controllers import PluginSessionController
from ouro.sdk.plugins import providers as provider_sdk
from ouro.tools import Tool, ToolExecutionContext, ToolInputRequest, ToolResult


def bind_generation_gate(
    prepared: PreparedContributionSet,
    gate: GenerationGate,
) -> PreparedContributionSet:
    return replace(
        prepared,
        tools=tuple(_gated_tool(item, gate) for item in prepared.tools),
        observers=tuple(_gated_observer(item, gate) for item in prepared.observers),
        context_providers=tuple(
            _gated_context_provider(item, gate) for item in prepared.context_providers
        ),
        commands=tuple(_gated_command(item, gate) for item in prepared.commands),
        compaction_strategies=tuple(
            _GatedCompactionStrategy(item, gate) for item in prepared.compaction_strategies
        ),
        providers=tuple(_gated_provider(item, gate) for item in prepared.providers),
        run_drivers=tuple(_gated_driver(item, gate) for item in prepared.run_drivers),
        continuation_policies=tuple(
            _gated_continuation(item, gate) for item in prepared.continuation_policies
        ),
        prompt_policies=tuple(
            _gated_prompt_policy(item, gate) for item in prepared.prompt_policies
        ),
        turn_policies=tuple(_gated_turn_policy(item, gate) for item in prepared.turn_policies),
        tool_policies=tuple(_gated_tool_policy(item, gate) for item in prepared.tool_policies),
        session_controllers=tuple(
            _gated_session_controller(item, gate) for item in prepared.session_controllers
        ),
    )


def _gated_provider(
    registration: ProviderRegistration, gate: GenerationGate
) -> ProviderRegistration:
    async def stream(
        request: provider_sdk.ProviderRequest, output: provider_sdk.ProviderOutput
    ) -> None:
        async with gate.invocation():
            await registration.stream(request, output)

    async def check(request: provider_sdk.ProviderRequest) -> provider_sdk.ProviderCheck:
        async with gate.invocation():
            return await registration.check(request)

    return replace(registration, stream=stream, check=check)


def _gated_session_controller(
    controller: PluginSessionController,
    gate: GenerationGate,
) -> PluginSessionController:
    async def activate(call):
        async with gate.invocation():
            return await _invoke_callback(controller.activate_handler, call)

    async def deactivate(call):
        async with gate.invocation():
            return await _invoke_callback(controller.deactivate_handler, call)

    return replace(
        controller,
        activate_handler=activate,
        deactivate_handler=deactivate,
    )


def _gated_tool(tool: Tool, gate: GenerationGate) -> Tool:
    async def invoke(
        invocation,
        context: ToolExecutionContext,
    ) -> ToolResult | ToolInputRequest:
        async with gate.invocation():
            return await tool.execute(invocation, context=context)

    async def resume(
        invocation,
        resolution,
        request,
        context: ToolExecutionContext,
    ):
        async with gate.invocation():
            return await tool.resume(
                invocation,
                resolution,
                request=request,
                context=context,
            )

    return Tool(
        definition=tool.definition,
        handler=invoke,
        resume_handler=resume if tool.resume_handler is not None else None,
    )


def _gated_observer(
    registration: ObserverRegistration,
    gate: GenerationGate,
) -> ObserverRegistration:
    async def invoke(observation: Observation) -> None:
        async with gate.invocation():
            result = await _invoke_callback(registration.handler, observation)
            if result is not None:
                raise TypeError("observer handler must return None")

    return replace(registration, handler=invoke)


def _gated_context_provider(
    registration: ContextProviderRegistration,
    gate: GenerationGate,
) -> ContextProviderRegistration:
    async def invoke(call: ContextProviderCall) -> ContextProviderResult:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, call)

    return replace(registration, handler=invoke)


def _gated_command(
    command: PluginCommand,
    gate: GenerationGate,
) -> PluginCommand:
    async def invoke(
        request: PluginCommandRequest,
    ) -> PluginCommandResult | str | Mapping[str, Any]:
        async with gate.invocation():
            return await _invoke_callback(command.handler, request)

    return replace(command, handler=invoke)


@dataclass
class _GatedCompactionStrategy:
    delegate: CompactionStrategy
    gate: GenerationGate
    strategy_id: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        self.strategy_id = self.delegate.strategy_id
        self.version = self.delegate.version

    async def compact(
        self,
        request: CompactionRequest,
        *,
        signal=None,
    ) -> CompactionArtifact:
        async with self.gate.invocation():
            return await self.delegate.compact(request, signal=signal)


def _gated_driver(
    registration: RunDriverRegistration, gate: GenerationGate
) -> RunDriverRegistration:
    async def invoke(
        context: RunDriverContext, resume: DriverResume | None
    ) -> DriverFinished | DriverSuspended:
        async with gate.invocation():
            return await registration.handler(context, resume)

    return replace(registration, handler=invoke)


def _gated_continuation(
    registration: ContinuationPolicyRegistration,
    gate: GenerationGate,
) -> ContinuationPolicyRegistration:
    async def invoke(outcome: RunOutcome) -> ContinuationDecision:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, outcome)

    return replace(registration, handler=invoke)


def _gated_tool_policy(
    registration: ToolPolicyRegistration,
    gate: GenerationGate,
) -> ToolPolicyRegistration:
    async def invoke(call: ToolPolicyCall) -> ToolPolicyDecision:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, call)

    return replace(registration, handler=invoke)


def _gated_prompt_policy(
    registration: PromptPolicyRegistration,
    gate: GenerationGate,
) -> PromptPolicyRegistration:
    async def invoke(call: PromptPolicyCall) -> PromptPolicyDecision:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, call)

    return replace(registration, handler=invoke)


def _gated_turn_policy(
    registration: TurnPolicyRegistration,
    gate: GenerationGate,
) -> TurnPolicyRegistration:
    async def invoke(candidate: TurnCompletionCandidate) -> TurnPolicyDecision:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, candidate)

    return replace(registration, handler=invoke)


async def _invoke_callback(handler: Callable[[Any], Any], argument: Any) -> Any:
    return await invoke_owned_callback(handler, argument)
