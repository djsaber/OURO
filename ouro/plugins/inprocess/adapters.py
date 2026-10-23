"""Adapt SDK execution and observation interfaces for the host."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ouro.core.callbacks import invoke_owned_callback
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionAbortedError, CompactionArtifact, CompactionRequest
from ouro.core.context_providers import (
    ContextFragment,
    ContextProviderCall,
    ContextProviderRegistration,
    ContextProviderResult,
)
from ouro.core.observers import (
    ContextPreparedObservation,
    Observation,
    ObserverRegistration,
    RunFinishedObservation,
    RuntimeEventObservation,
    ToolCallFinishedObservation,
    ToolCallRequestedObservation,
)
from ouro.core.session_controls import (
    SessionControlClaim,
    SessionControlTransitionRequest,
    SessionInputBatchRequest,
    SessionInputRequest,
)
from ouro.plugins.harness_calls import bind_context_callback, invoke_plugin_callback
from ouro.plugins.inprocess.policies import (
    _adapt_continuation_policy,
    _adapt_prompt_policy,
    _adapt_tool_policy,
    _adapt_turn_policy,
)
from ouro.plugins.inprocess.tools import _adapt_tool
from ouro.plugins.providers import adapt_provider
from ouro.plugins.registry import PluginCommand, PluginCommandRequest, PluginCommandResult
from ouro.plugins.run_drivers import adapt_run_driver
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerCall,
    PluginSessionControllerOutput,
)
from ouro.sdk.plugins import v5 as sdk


@dataclass(frozen=True)
class SDKContributionAdapter:
    attribute: str
    adapt: Callable[..., Any]


def _adapt_observer(
    contribution: sdk.ObserverContribution, *, owner_id: str
) -> ObserverRegistration:
    async def handler(observation: Observation) -> None:
        raw, change = await invoke_plugin_callback(
            contribution.handler,
            _sdk_observation(observation),
            snapshot=observation.harness,
            owner_id=owner_id,
            writable=False,
        )
        if raw is not None:
            raise TypeError("observer handler must return None")

    return ObserverRegistration(
        plugin_id="",
        observer_id=contribution.id,
        event=contribution.event,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        event_types=tuple(contribution.event_types),
        active_when_controller_id=contribution.active_when,
    )


def _sdk_observation(observation: Observation) -> sdk.Observation:
    if isinstance(observation, RuntimeEventObservation):
        record = observation.record
        return sdk.RuntimeEventObservation(
            session_id=observation.session_id,
            run_id=observation.run_id,
            record=sdk.RuntimeEventView(
                event_id=record.event_id,
                session_id=record.session_id,
                run_id=record.run_id,
                turn_id=record.turn_id,
                event_type=record.event_type,
                created_at=record.created_at,
                sequence=record.sequence,
                history_epoch=record.history_epoch,
                payload=record.payload,
                metadata=record.metadata,
            ),
        )
    if isinstance(observation, ContextPreparedObservation):
        return sdk.ContextPreparedObservation(
            session_id=observation.session_id,
            run_id=observation.run_id,
            message_count=observation.message_count,
            tool_count=observation.tool_count,
            model_round=observation.model_round,
            context_metadata=observation.context_metadata,
        )
    if isinstance(observation, ToolCallRequestedObservation):
        return sdk.ToolCallRequestedObservation(
            session_id=observation.session_id,
            run_id=observation.run_id,
            tool_call_id=observation.tool_call_id,
            tool_name=observation.tool_name,
            canonical_tool_id=observation.canonical_tool_id,
            owner_id=observation.owner_id,
            effect=observation.effect,
            batch_behavior=observation.batch_behavior,
            arguments=observation.arguments,
        )
    if isinstance(observation, ToolCallFinishedObservation):
        return sdk.ToolCallFinishedObservation(
            session_id=observation.session_id,
            run_id=observation.run_id,
            tool_call_id=observation.tool_call_id,
            tool_name=observation.tool_name,
            canonical_tool_id=observation.canonical_tool_id,
            owner_id=observation.owner_id,
            effect=observation.effect,
            batch_behavior=observation.batch_behavior,
            is_error=observation.is_error,
            content=observation.content,
            structured=observation.structured,
            resource_refs=observation.resource_refs,
            exit_code=observation.exit_code,
        )
    if isinstance(observation, RunFinishedObservation):
        outcome = observation.outcome
        return sdk.RunFinishedObservation(
            session_id=observation.session_id,
            run_id=observation.run_id,
            outcome=sdk.RunOutcome(
                session_id=outcome.session_id,
                run_id=outcome.run_id,
                status=outcome.status,
                origin=outcome.origin,
                final_text=outcome.final_text,
                error=outcome.error,
                usage=outcome.usage,
            ),
        )
    raise TypeError(f"unsupported observation: {type(observation).__name__}")


def _adapt_command(contribution: sdk.CommandContribution, *, owner_id: str) -> PluginCommand:
    async def handler(request: PluginCommandRequest) -> PluginCommandResult:
        raw, change = await invoke_plugin_callback(
            contribution.handler,
            sdk.CommandCall(
                command_id=request.command_id,
                argument=request.argument,
                session_id=request.session_id,
                workspace=request.workspace,
            ),
            snapshot=request.harness,
            owner_id=owner_id,
            writable=True,
        )
        if isinstance(raw, str):
            return PluginCommandResult(
                command_id=request.command_id, output=raw, harness_change=change
            )
        if isinstance(raw, sdk.CommandOutput):
            return PluginCommandResult(
                harness_change=change,
                command_id=request.command_id,
                output=raw.output,
                run_action=raw.run_action,
                data=dict(raw.data),
                session_inputs=[
                    SessionInputRequest(
                        prompt=item.prompt,
                        mode=item.mode,
                        title=item.title,
                        role=item.role,
                        scaffold=item.scaffold,
                        semantic_tag=item.semantic_tag,
                        driver_id=item.driver_id,
                    )
                    for item in raw.session_inputs
                ],
                session_input_batches=[
                    SessionInputBatchRequest(
                        inputs=tuple(
                            SessionInputRequest(
                                prompt=item.prompt,
                                mode=item.mode,
                                title=item.title,
                                role=item.role,
                                scaffold=item.scaffold,
                                semantic_tag=item.semantic_tag,
                                driver_id=item.driver_id,
                            )
                            for item in batch.inputs
                        ),
                        coalesce_key=batch.coalesce_key,
                    )
                    for batch in raw.session_input_batches
                ],
                session_control_transitions=[
                    SessionControlTransitionRequest(
                        action=item.action,
                        controller_id=item.controller_id,
                        activation_id=item.activation_id,
                        replace_activation_ids=list(item.replace_activation_ids),
                        expected_session_revision=item.expected_session_revision,
                        expected_activation_revision=item.expected_activation_revision,
                        payload=item.payload,
                        reason=item.reason,
                    )
                    for item in raw.session_control_transitions
                ],
            )
        return PluginCommandResult(
            command_id=request.command_id, data=dict(raw), harness_change=change
        )

    return PluginCommand(
        name=contribution.id,
        description=contribution.description,
        usage=contribution.usage,
        handler=handler,
        timeout_seconds=contribution.timeout_seconds,
    )


def _adapt_session_controller(
    contribution: sdk.SessionControllerContribution,
    *,
    owner_id: str,
) -> PluginSessionController:
    async def invoke(handler, call: PluginSessionControllerCall):
        raw, change = await invoke_plugin_callback(
            handler,
            sdk.SessionControllerCall(
                session_id=call.session_id,
                session_kind=call.session_kind,
                activation_id=call.activation_id,
                controller_id=call.controller_id,
                action=call.action,
                replacement_controller_id=call.replacement_controller_id,
                payload=call.payload,
                reason=call.reason,
                workspace=call.workspace,
            ),
            snapshot=call.harness,
            owner_id=owner_id,
            writable=True,
        )
        if not isinstance(raw, sdk.SessionControllerOutput):
            raise TypeError("session controller handler must return SessionControllerOutput")
        return PluginSessionControllerOutput(
            harness_change=change,
            output=raw.output,
            data=dict(raw.data),
            usage_scope=raw.usage_scope,
        )

    return PluginSessionController(
        controller_id=contribution.id,
        description=contribution.description,
        claims=tuple(
            SessionControlClaim(domain=claim.domain, access=claim.access)
            for claim in contribution.claims
        ),
        activate_handler=lambda call: invoke(contribution.activate, call),
        deactivate_handler=lambda call: invoke(contribution.deactivate, call),
        timeout_seconds=contribution.timeout_seconds,
    )


def _adapt_context(
    contribution: sdk.ContextContribution, *, owner_id: str
) -> ContextProviderRegistration:
    async def handler(call: ContextProviderCall) -> ContextProviderResult:
        raw, change = await invoke_plugin_callback(
            contribution.handler,
            sdk.ContextCall(
                session_id=call.session_id,
                run_id=call.run_id,
                message_count=call.message_count,
                model_round=call.model_round,
                workspace=call.workspace,
            ),
            snapshot=call.harness,
            owner_id=owner_id,
            writable=False,
        )
        if isinstance(raw, sdk.ContextOutput):
            values = raw.fragments
        elif isinstance(raw, str):
            values = (raw,)
        else:
            values = raw
        return ContextProviderResult(
            fragments=[
                ContextFragment(
                    text=value.text,
                    persistence=value.persistence,
                    target=value.target,
                    role=value.role,
                    scaffold=value.scaffold,
                    semantic_tag=value.semantic_tag,
                )
                if isinstance(value, sdk.ContextFragment)
                else ContextFragment(text=value)
                for value in values
                if value
            ]
        )

    return ContextProviderRegistration(
        plugin_id="",
        provider_id=contribution.id,
        handler=handler,
        priority=contribution.priority,
        timeout_seconds=contribution.timeout_seconds,
        failure_mode=contribution.failure_mode,
        active_when_controller_id=contribution.active_when,
    )


class _SDKCompactionStrategy:
    def __init__(self, contribution: sdk.CompactionContribution, *, owner_id: str) -> None:
        self._owner_id = owner_id
        self.strategy_id = contribution.id
        self.version = contribution.version
        self._handler = bind_context_callback(contribution.handler)

    async def compact(
        self,
        request: CompactionRequest,
        *,
        signal: AbortSignal | None = None,
    ) -> CompactionArtifact:
        context = _SDKCompactionCancellationContext(signal)
        context.raise_if_aborted()
        call = sdk.CompactionCall(
            session_id=request.session_id,
            run_id=request.run_id,
            agent_system_prompt=request.agent_system_prompt,
            previous_summary=request.previous_summary,
            messages=tuple(
                message.model_dump(mode="json", exclude_none=True) for message in request.messages
            ),
        )
        raw, _ = await invoke_plugin_callback(
            self._handler,
            call,
            snapshot=request.harness,
            owner_id=self._owner_id,
            writable=False,
            signal=signal,
            context=context,
        )
        context.raise_if_aborted()
        return CompactionArtifact(summary=raw.summary, usage=dict(raw.usage))


class _SDKCompactionCancellationContext:
    def __init__(self, signal: AbortSignal | None, *, poll_interval: float = 0.05) -> None:
        self._signal = signal
        self._poll_interval = poll_interval

    @property
    def is_aborted(self) -> bool:
        return self._signal.is_aborted() if self._signal is not None else False

    @property
    def abort_reason(self) -> str:
        return self._signal.reason if self._signal is not None else "Compaction aborted"

    def raise_if_aborted(self) -> None:
        if self.is_aborted:
            raise CompactionAbortedError(self.abort_reason)

    async def sleep(self, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            self.raise_if_aborted()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self._poll_interval, remaining))


class _SDKBackgroundService:
    def __init__(self, contribution: sdk.BackgroundServiceContribution, *, owner_id: str) -> None:
        del owner_id
        self.service_id = contribution.id
        self._start = contribution.start
        self._stop = contribution.stop

    async def start(self) -> None:
        await invoke_owned_callback(self._start)

    async def stop(self) -> None:
        await invoke_owned_callback(self._stop)


PROVIDER_SDK_ADAPTER = SDKContributionAdapter("providers", adapt_provider)


RUN_DRIVER_SDK_ADAPTER = SDKContributionAdapter("run_drivers", adapt_run_driver)


TOOL_SDK_ADAPTER = SDKContributionAdapter("tools", _adapt_tool)


OBSERVER_SDK_ADAPTER = SDKContributionAdapter("observers", _adapt_observer)


COMMAND_SDK_ADAPTER = SDKContributionAdapter("commands", _adapt_command)


CONTEXT_SDK_ADAPTER = SDKContributionAdapter("context_providers", _adapt_context)


COMPACTION_SDK_ADAPTER = SDKContributionAdapter(
    "compaction_strategies",
    _SDKCompactionStrategy,
)


BACKGROUND_SERVICE_SDK_ADAPTER = SDKContributionAdapter(
    "background_services",
    _SDKBackgroundService,
)


CONTINUATION_SDK_ADAPTER = SDKContributionAdapter(
    "continuation_policies",
    _adapt_continuation_policy,
)


PROMPT_POLICY_SDK_ADAPTER = SDKContributionAdapter(
    "prompt_policies",
    _adapt_prompt_policy,
)


TOOL_POLICY_SDK_ADAPTER = SDKContributionAdapter("tool_policies", _adapt_tool_policy)


TURN_POLICY_SDK_ADAPTER = SDKContributionAdapter("turn_policies", _adapt_turn_policy)


SESSION_CONTROLLER_SDK_ADAPTER = SDKContributionAdapter(
    "session_controllers",
    _adapt_session_controller,
)
