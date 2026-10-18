from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from ouro.core.redaction import SensitiveValueRedactor
from ouro.local_locks import FileAccessLock
from ouro.plugins.api import LoadedPluginModule
from ouro.plugins.candidates import PluginRuntimeCandidate
from ouro.plugins.catalog import ContributionBinding, GenerationGate, acquire_generations
from ouro.plugins.contributions import (
    BackgroundServiceScope,
    PreparedBackgroundService,
    PreparedContributionSet,
)
from ouro.plugins.models import PluginDiagnostic, PluginGenerationRecord
from ouro.plugins.resources import OwnedTaskGroup, ScopedPluginDataStore
from ouro.plugins.services import ServiceEndpoint
from ouro.plugins.tool_groups import PluginToolGroups
from ouro.sdk.plugins.v5 import StopReason


@dataclass
class PluginGeneration:
    """Own one loaded implementation until all accepted work and resources settle."""

    record: PluginGenerationRecord
    candidate: PluginRuntimeCandidate
    instance: Any
    contributions: PreparedContributionSet
    gate: GenerationGate
    tasks: OwnedTaskGroup
    redactor: SensitiveValueRedactor
    dependencies: tuple[PluginGeneration, ...] = ()
    data_store: ScopedPluginDataStore | None = None
    data_lock: FileAccessLock | None = None
    module: LoadedPluginModule | None = None
    services: ServiceEndpoint | None = None
    tool_groups: PluginToolGroups | None = None
    resources: list[Any] = field(default_factory=list)
    diagnostics: list[PluginDiagnostic] = field(default_factory=list)
    lease_count: int = 0
    retiring: bool = False
    stopped: bool = False
    retirement_ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    service_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    started_services: list[PreparedBackgroundService] = field(default_factory=list, repr=False)
    cleanup: asyncio.Task[None] | None = field(default=None, repr=False)
    cleanup_error: Exception | None = field(default=None, repr=False)
    _instance_stopped: bool = field(default=False, repr=False)

    def dependency_closure(self) -> tuple[PluginGeneration, ...]:
        """Return exact dependencies before their owner, once per generation."""
        ordered: list[PluginGeneration] = []
        seen: set[str] = set()
        pending = [(self, False)]
        while pending:
            generation, expanded = pending.pop()
            if expanded:
                ordered.append(generation)
            elif generation.record.generation_id not in seen:
                seen.add(generation.record.generation_id)
                pending.append((generation, True))
                pending.extend((item, False) for item in reversed(generation.dependencies))
        return tuple(ordered)

    def bind(self) -> ContributionBinding[PluginGeneration]:
        return ContributionBinding(self, acquire_generations(self.dependency_closure()))

    def acquire(self) -> PluginGenerationLease:
        if self.stopped:
            raise RuntimeError(f"plugin generation is stopped: {self.record.generation_id}")
        self.lease_count += 1
        if self.gate.is_open:
            self.retirement_ready.clear()
        return PluginGenerationLease(self)

    def release(self) -> None:
        self.lease_count -= 1
        if self.lease_count == 0:
            self.retirement_ready.set()

    def revoke(self, *, cancel: bool = False) -> None:
        self.gate.close()
        if cancel:
            self.gate.cancel_inflight()
        self.record.status = "stopping"
        # A revoked directory cannot start callbacks. Only executing callbacks,
        # not dormant catalog references, need to settle before resource cleanup.
        self.retirement_ready.set()

    def describe_error(self, error: Exception) -> str:
        messages: list[str] = []
        pending = [error]
        while pending:
            cause = pending.pop()
            messages.append(self.redactor.redact(f"{type(cause).__name__}: {cause}"))
            if isinstance(cause, ExceptionGroup):
                pending.extend(reversed(cause.exceptions))
        return "\n".join(messages)[:4096]

    async def start_services(self, *, scopes: frozenset[BackgroundServiceScope]) -> None:
        async with self.service_lock:
            if self.stopped:
                return
            for service in self.contributions.background_services:
                if service.scope not in scopes or service in self.started_services:
                    continue
                # start() may acquire a resource before failing.
                self.started_services.append(service)
                await service.start()

    async def stop_services(
        self, *, scopes: frozenset[BackgroundServiceScope] | None = None
    ) -> None:
        async with self.service_lock:
            errors: list[Exception] = []
            for service in reversed(tuple(self.started_services)):
                if scopes is not None and service.scope not in scopes:
                    continue
                try:
                    await service.stop()
                except Exception as exc:  # Keep failed owners available for explicit cleanup.
                    errors.append(exc)
                else:
                    self.started_services.remove(service)
            if errors:
                self.cleanup_error = ExceptionGroup(
                    "plugin background services did not stop", errors
                )
                raise self.cleanup_error

    async def stop(self, *, reason: StopReason, timeout: float) -> None:
        async with self.stop_lock:
            if self.stopped:
                return
            self.revoke()
            await self.gate.wait_drained()
            errors: list[Exception] = []
            try:
                await self.stop_services()
            except Exception as exc:
                errors.append(exc)
            if self.services is not None:
                try:
                    await self.services.close()
                except Exception as exc:
                    errors.append(exc)
            if self.tool_groups is not None:
                await self.tool_groups.close()
            if not self._instance_stopped:
                try:
                    await stop_instance(self.instance, reason=reason, timeout=timeout)
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._instance_stopped = True
            try:
                await self.tasks.close(timeout=timeout)
            except Exception as exc:
                errors.append(exc)
            for resource in reversed(tuple(self.resources)):
                try:
                    await resource.close()
                except Exception as exc:
                    errors.append(exc)
                else:
                    self.resources.remove(resource)
            if errors:
                self.cleanup_error = ExceptionGroup(
                    f"plugin generation shutdown failed: {self.record.generation_id}", errors
                )
                self.record.status = "failed"
                raise self.cleanup_error
            if self.data_store is not None:
                self.data_store.close()
            if self.module is not None:
                self.module.close()
            if self.data_lock is not None:
                self.data_lock.release()
            self.cleanup_error = None
            self.record.status = "disabled"
            self.stopped = True


def revoke_dependent_generations(
    generations: Sequence[PluginGeneration], generation_ids: set[str]
) -> None:
    """Revoke accepted work using its actual graph, including unpublished versions."""

    if not generation_ids:
        return
    by_id = {item.record.generation_id: item for item in generations}
    dependents: dict[str, list[str]] = {}
    for item in generations:
        for dependency in item.dependencies:
            dependents.setdefault(dependency.record.generation_id, []).append(
                item.record.generation_id
            )
    pending = list(generation_ids & by_id.keys())
    revoked: set[str] = set()
    while pending:
        generation_id = pending.pop()
        if generation_id in revoked:
            continue
        revoked.add(generation_id)
        by_id[generation_id].revoke(cancel=True)
        pending.extend(dependents.get(generation_id, ()))


class PluginGenerationLease:
    def __init__(self, generation: PluginGeneration) -> None:
        self._generation = generation
        self._closed = False

    @property
    def record(self) -> PluginGenerationRecord:
        return self._generation.record

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._generation.release()

    def __enter__(self) -> PluginGenerationLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


async def stop_instance(instance: Any, *, reason: StopReason, timeout: float) -> None:
    await asyncio.wait_for(instance.stop(reason, monotonic() + timeout), timeout=timeout)
