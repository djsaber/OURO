"""Adapt worker contributions to host execution interfaces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ouro.plugins.worker.codec import _worker_schema
from ouro.plugins.worker.instance import WorkerPluginInstance
from ouro.sdk.plugins import v5 as sdk

WorkerContributionBuilder = Callable[
    [WorkerPluginInstance, object, dict[str, set[str]]],
    list[Any],
]


@dataclass(frozen=True)
class WorkerContributionAdapter:
    kind: str
    attribute: str
    build: WorkerContributionBuilder

    def prepare(
        self,
        instance: WorkerPluginInstance,
        raw: object,
        available: dict[str, set[str]],
    ) -> list[Any]:
        return self.build(instance, raw, available)


def _worker_tools(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    return [
        sdk.ToolContribution(
            id=item.id,
            exposed_name=item.exposed_name,
            description=item.description,
            permission_mode=item.permission_mode,
            permission_control=item.permission_control,
            effect=item.effect,
            batch_behavior=item.batch_behavior,
            parameters_schema=_worker_schema(raw, "tools", item.id, "parameters_schema"),
            result_schema=_worker_schema(raw, "tools", item.id, "result_schema"),
            handler=instance._tool_handler(item.id),
            resume_handler=(instance._tool_resume_handler(item.id) if item.resumable else None),
            supports_unattended=item.supports_unattended,
            active_when=item.active_when,
            interaction_controller=item.interaction_controller,
        )
        for item in instance.manifest.contributes.tools
        if item.id in available.get("tools", set())
    ]


def _worker_observers(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.ObserverContribution(
            id=item.id,
            event=item.event,
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            event_types=tuple(item.event_types),
            active_when=item.active_when,
            handler=instance._observer_handler(item.id),
        )
        for item in instance.manifest.contributes.observers
        if item.id in available.get("observers", set())
    ]


def _worker_commands(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.CommandContribution(
            id=item.id,
            description=item.description,
            handler=instance._command_handler(item.id),
        )
        for item in instance.manifest.contributes.commands
        if item.id in available.get("commands", set())
    ]


def _worker_session_controllers(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.SessionControllerContribution(
            id=item.id,
            description=item.description,
            claims=tuple(
                sdk.SessionControlClaim(domain=claim.domain, access=claim.access)
                for claim in item.claims
            ),
            activate=instance._session_controller_handler(item.id),
            deactivate=instance._session_controller_handler(item.id),
            timeout_seconds=item.timeout_seconds,
        )
        for item in instance.manifest.contributes.session_controllers
        if item.id in available.get("session_controllers", set())
    ]


def _worker_context_providers(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.ContextContribution(
            id=item.id,
            handler=instance._context_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            failure_mode=item.failure_mode,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.context_providers
        if item.id in available.get("context_providers", set())
    ]


def _worker_compaction_strategies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.CompactionContribution(
            id=item.id,
            version="1",
            handler=instance._compaction_handler(item.id),
        )
        for item in instance.manifest.contributes.compaction_strategies
        if item.id in available.get("compaction_strategies", set())
    ]


def _worker_background_services(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.BackgroundServiceContribution(
            id=item.id,
            start=instance._background_handler(item.id, start=True),
            stop=instance._background_handler(item.id, start=False),
        )
        for item in instance.manifest.contributes.background_services
        if item.id in available.get("background_services", set())
    ]


def _worker_continuation_policies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.ContinuationPolicyContribution(
            id=item.id,
            handler=instance._continuation_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.continuation_policies
        if item.id in available.get("continuation_policies", set())
    ]


def _worker_tool_policies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.ToolPolicyContribution(
            id=item.id,
            handler=instance._tool_policy_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.tool_policies
        if item.id in available.get("tool_policies", set())
    ]


def _worker_prompt_policies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.PromptPolicyContribution(
            id=item.id,
            handler=instance._prompt_policy_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.prompt_policies
        if item.id in available.get("prompt_policies", set())
    ]


def _worker_turn_policies(
    instance: WorkerPluginInstance,
    raw: object,
    available: dict[str, set[str]],
) -> list[Any]:
    del raw
    return [
        sdk.TurnPolicyContribution(
            id=item.id,
            handler=instance._turn_policy_handler(item.id),
            priority=item.priority,
            timeout_seconds=item.timeout_seconds,
            active_when=item.active_when,
        )
        for item in instance.manifest.contributes.turn_policies
        if item.id in available.get("turn_policies", set())
    ]


TOOL_WORKER_ADAPTER = WorkerContributionAdapter("tools", "tools", _worker_tools)


OBSERVER_WORKER_ADAPTER = WorkerContributionAdapter(
    "observers",
    "observers",
    _worker_observers,
)


COMMAND_WORKER_ADAPTER = WorkerContributionAdapter(
    "commands",
    "commands",
    _worker_commands,
)


CONTEXT_WORKER_ADAPTER = WorkerContributionAdapter(
    "context_providers",
    "context_providers",
    _worker_context_providers,
)


COMPACTION_WORKER_ADAPTER = WorkerContributionAdapter(
    "compaction_strategies",
    "compaction_strategies",
    _worker_compaction_strategies,
)


BACKGROUND_SERVICE_WORKER_ADAPTER = WorkerContributionAdapter(
    "background_services",
    "background_services",
    _worker_background_services,
)


def _worker_run_drivers(
    instance: WorkerPluginInstance, raw: object, available: dict[str, set[str]]
) -> list[Any]:
    return [
        sdk.RunDriverContribution(item.id, instance.driver_bridge.handler(item.id))
        for item in instance.manifest.contributes.run_drivers
        if item.id in available.get("run_drivers", set())
    ]


def _worker_providers(
    instance: WorkerPluginInstance, raw: object, available: dict[str, set[str]]
) -> list[Any]:
    return [
        instance.provider_bridge.contribution(item.id)
        for item in instance.manifest.contributes.providers
        if item.id in available.get("providers", set())
    ]


PROVIDER_WORKER_ADAPTER = WorkerContributionAdapter("providers", "providers", _worker_providers)


RUN_DRIVER_WORKER_ADAPTER = WorkerContributionAdapter(
    "run_drivers", "run_drivers", _worker_run_drivers
)


CONTINUATION_WORKER_ADAPTER = WorkerContributionAdapter(
    "continuation_policies",
    "continuation_policies",
    _worker_continuation_policies,
)


PROMPT_POLICY_WORKER_ADAPTER = WorkerContributionAdapter(
    "prompt_policies",
    "prompt_policies",
    _worker_prompt_policies,
)


TOOL_POLICY_WORKER_ADAPTER = WorkerContributionAdapter(
    "tool_policies",
    "tool_policies",
    _worker_tool_policies,
)


TURN_POLICY_WORKER_ADAPTER = WorkerContributionAdapter(
    "turn_policies",
    "turn_policies",
    _worker_turn_policies,
)


SESSION_CONTROLLER_WORKER_ADAPTER = WorkerContributionAdapter(
    "session_controllers",
    "session_controllers",
    _worker_session_controllers,
)
