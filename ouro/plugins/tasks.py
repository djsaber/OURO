from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from pydantic import JsonValue

from ouro.core.plugin_tasks import PluginTaskCall, PluginTaskTarget
from ouro.plugins.catalog.gates import (
    GenerationLease,
    PluginGenerationUnavailableError,
    acquire_generations,
)
from ouro.plugins.implementation_contracts import plugin_implementation
from ouro.plugins.lifecycle.generation import PluginGeneration
from ouro.plugins.services import ServiceEndpoint
from ouro.sdk.plugins.scheduled import ScheduledInvocation


@dataclass
class PluginTaskBinding:
    target: PluginTaskCall
    endpoint: ServiceEndpoint
    leases: tuple[GenerationLease, ...]

    async def invoke(self, *, task_id: str, execution_id: str, scheduled_at: datetime) -> JsonValue:
        if not self.leases:
            raise RuntimeError("Plugin task binding is closed")
        request = self.target.request
        try:
            return await self.endpoint.call_scheduled(
                request.service,
                request.method,
                request.arguments,
                ScheduledInvocation(task_id, execution_id, scheduled_at),
            )
        except PluginGenerationUnavailableError as exc:
            raise asyncio.CancelledError("Plugin task provider was revoked") from exc

    def close(self) -> None:
        leases, self.leases = self.leases, ()
        for lease in reversed(leases):
            lease.close()


def bind_plugin_task(generation: PluginGeneration, target: PluginTaskTarget) -> PluginTaskBinding:
    if not generation.gate.is_open or generation.services is None:
        raise ValueError(f"Plugin task provider is unavailable: {target.plugin_id}")
    if "background.run" not in {item.id for item in generation.candidate.manifest.permissions}:
        raise PermissionError("Scheduled plugin work requires background.run")
    arguments = generation.services.validate_scheduled(
        target.service, target.method, target.arguments
    )
    closure = {item.record.plugin_id: item for item in generation.dependency_closure()}
    for item in closure.values():
        if not item.gate.is_open:
            raise ValueError(f"Plugin task dependency is unavailable: {item.record.plugin_id}")
    accepted = PluginTaskCall(
        request=target.model_copy(update={"arguments": arguments}, deep=True),
        implementation=plugin_implementation(generation.record),
        dependencies=tuple(
            plugin_implementation(closure[plugin_id].record)
            for plugin_id in sorted(closure)
            if plugin_id != target.plugin_id
        ),
    )
    return PluginTaskBinding(accepted, generation.services, acquire_generations(closure.values()))
