from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from ouro.core.base import new_id
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import settle_owned_tasks
from ouro.core.model_generation import ModelGenerationPort
from ouro.core.plugin_tasks import PluginTaskTarget
from ouro.core.redaction import SensitiveValueRedactor, redact_text
from ouro.core.run_drivers import DEFAULT_DRIVER_ID, RunDriverBinding
from ouro.core.task_ports import TaskServicePort
from ouro.core.tasks import AgentTaskTarget
from ouro.plugins.api import (
    BoundCredentialResolver,
    adapt_sdk_contributions,
    build_plugin_context,
    load_plugin_factory,
)
from ouro.plugins.builtin import EMPTY_BUILTIN_PLUGIN_REGISTRY, BuiltinPluginRegistry
from ouro.plugins.candidates import PluginRuntimeCandidate
from ouro.plugins.catalog import (
    CatalogContributionError,
    ContributionBinding,
    ExtensionCatalog,
    ExtensionCatalogBase,
    ExtensionCatalogLease,
    GenerationGate,
    GenerationLease,
    bind_generation_gate,
    build_extension_catalog,
)
from ouro.plugins.contributions import (
    BackgroundServiceScope,
    ContributionCoordinator,
    ContributionOwner,
    PluginContributions,
    PreparedContributionSet,
    bind_execution_contract,
)
from ouro.plugins.dependencies import (
    LockedPluginDependency,
    PluginDependencyError,
    PluginLockGraph,
    active_dependents,
    builtin_desired_enabled,
    desired_dependant_closure,
    plan_desired_plugin_activation,
    resolve_plugin_dependencies,
)
from ouro.plugins.driver_contracts import resolve_driver_contract
from ouro.plugins.environment import PluginEnvironmentService
from ouro.plugins.execution_contracts import (
    build_dependency_lock_fingerprint,
    build_execution_contract_hash,
)
from ouro.plugins.generation import PluginGeneration, revoke_dependent_generations
from ouro.plugins.generation_resources import (
    GenerationResourceRequest,
    prepare_generation_resources,
)
from ouro.plugins.implementation_contracts import plugin_implementation
from ouro.plugins.instructions import AgentInstructionLoader
from ouro.plugins.manifest import plugin_module_namespace
from ouro.plugins.models import (
    BuiltinPluginActivationOverride,
    PluginAuditEvent,
    PluginDiagnostic,
    PluginGenerationRecord,
    PluginRecord,
    PluginStateDocument,
    PluginStatus,
    create_configuration_revision,
)
from ouro.plugins.native_ports import PluginNativePorts
from ouro.plugins.package import PLUGIN_API_VERSION
from ouro.plugins.processes import PluginProcessService
from ouro.plugins.providers import ProviderBinding
from ouro.plugins.readiness import plugin_install_readiness
from ouro.plugins.resources import OwnedTaskGroup, ScopedPluginDataStore
from ouro.plugins.run_drivers import RunDriverRegistration
from ouro.plugins.scheduling import NativePluginScheduler
from ouro.plugins.services import ServiceEndpoint
from ouro.plugins.store import PluginPackageStore, PluginStateConflictError
from ouro.plugins.tasks import PluginTaskBinding, bind_plugin_task
from ouro.plugins.tool_groups import PluginToolGroups, ToolGroupPublication, ToolGroupVersion
from ouro.plugins.worker import WorkerPluginInstance
from ouro.sdk.plugins import v5 as sdk
from ouro.sdk.plugins.v5 import StopReason
from ouro.sessions.base import SessionRepository
from ouro.skills import SkillMetadata


class PluginGenerationStartError(RuntimeError):
    pass


class PluginCandidateBuildError(RuntimeError):
    def __init__(self, plugin_id: str, cause: Exception) -> None:
        super().__init__(f"candidate generation failed for {plugin_id}: {cause}")
        self.plugin_id = plugin_id
        self.cause = cause


@dataclass
class _LifecycleAdmission:
    catalog: ExtensionCatalog | None


class PluginRuntimeResource:
    """Own activated plugin generations and their complete shutdown boundary."""

    def __init__(
        self,
        store: PluginPackageStore,
        coordinator: ContributionCoordinator,
        *,
        credential_resolver=None,
        stop_timeout_seconds: float = 5.0,
        builtin_plugins: BuiltinPluginRegistry = EMPTY_BUILTIN_PLUGIN_REGISTRY,
        base_skills: tuple[SkillMetadata, ...] = (),
        session_repository: SessionRepository | None = None,
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.credential_resolver = credential_resolver
        self.stop_timeout_seconds = stop_timeout_seconds
        self.builtin_plugins = builtin_plugins
        self.builtin_contracts = builtin_plugins.contracts
        self.base_skills = tuple(skill.model_copy(deep=True) for skill in base_skills)
        self.session_repository = session_repository
        self._task_service: TaskServicePort | None = None
        self._model_generation: ModelGenerationPort | None = None
        self._instruction_loader = AgentInstructionLoader(ouro_home=store.home)
        self._active: dict[str, PluginGeneration] = {}
        self._retired: dict[str, PluginGeneration] = {}
        self._failed: dict[str, str] = {}
        self._diagnostics: list[PluginDiagnostic] = []
        self._mutation_lock = asyncio.Lock()
        self._catalog_pointer_lock = threading.Lock()
        self._catalog_base: ExtensionCatalogBase | None = None
        self._current_catalog: ExtensionCatalog | None = None
        self._retirement_tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._observed_state_stamp: tuple[int, int, int] | None = None
        self._home_services_owner = False
        self._lifecycle_admission: ContextVar[_LifecycleAdmission | None] = ContextVar(
            "ouro_lifecycle_admission", default=None
        )

    @contextmanager
    def _admit_lifecycle_callbacks(self) -> Iterator[None]:
        """Nested operations use the directory already committed by this lifecycle."""
        admission = _LifecycleAdmission(self.catalog())
        token = self._lifecycle_admission.set(admission)
        try:
            yield
        finally:
            # Spawned background tasks must synchronize future independent work,
            # and must not retain this catalog after the admitting callback ends.
            admission.catalog = None
            self._lifecycle_admission.reset(token)

    async def bind_provider(self, protocol: str) -> ProviderBinding:
        admission = self._lifecycle_admission.get()
        if admission is not None and admission.catalog is not None:
            return admission.catalog.bind_provider(protocol)
        await self.synchronize()
        return self.catalog().bind_provider(protocol)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        if self._started:
            return
        self._ensure_catalog()
        state_stamp = self.store.state_stamp()
        recovery = self.store.recover_state()
        self._started = True
        if recovery.quarantine_path is not None:
            self._diagnostics.append(
                PluginDiagnostic(
                    code="plugin_state_reset",
                    phase="startup",
                    message=(
                        "plugin state was unreadable and has been quarantined: "
                        f"{redact_text(recovery.error or 'unknown error')}"
                    ),
                    suggested_action=(
                        f"Inspect {recovery.quarantine_path} and reinstall any plugin "
                        "that should be active."
                    ),
                )
            )
            self.store.append_audit(
                PluginAuditEvent(
                    action="state_reset",
                    detail=(
                        f"unreadable plugin state quarantined to {recovery.quarantine_path.name}"
                    ),
                )
            )
        state = recovery.state
        async with self._mutation_lock:
            plan = plan_desired_plugin_activation(state, builtin_contracts=self.builtin_contracts)
            eligible: set[str] = set()
            for node in plan.graph.nodes:
                if (
                    set(node.dependencies) <= eligible
                    and self._candidate_readiness(state, node.plugin_id) == "ready"
                ):
                    eligible.add(node.plugin_id)
            graph = PluginLockGraph(
                tuple(node for node in plan.graph.nodes if node.plugin_id in eligible)
            )
            try:
                if eligible - self._active.keys():
                    await self._transition_state_locked(
                        expected=state,
                        candidate_state=state,
                        rebuild_ids=eligible - self._active.keys(),
                        startup_graph=graph,
                        reason="activate",
                    )
            except Exception as exc:  # noqa: BLE001 - startup can expose a usable empty catalog.
                self._diagnostics.append(
                    PluginDiagnostic(
                        code="plugin_startup_failed",
                        phase="startup",
                        message=f"{type(exc).__name__}: {redact_text(str(exc))}",
                        suggested_action="Review plugin diagnostics and retry activation.",
                    )
                )

        self._observed_state_stamp = state_stamp

    async def start_home_services(self) -> None:
        """Start home-scoped work only while the caller holds the scheduler lock."""

        async with self._mutation_lock:
            if self._closed:
                raise RuntimeError("plugin runtime is closed")
            if self._home_services_owner:
                return
            self._home_services_owner = True
            for generation in self._active.values():
                await self._start_generation_services(generation, scopes=frozenset({"home"}))

    async def stop_home_services(self) -> None:
        """Settle singleton work before the scheduler releases its home lock."""

        async with self._mutation_lock:
            self._home_services_owner = False
            errors: list[Exception] = []
            generations = (*self._active.values(), *self._retired.values())
            for generation in reversed(generations):
                try:
                    with self._admit_lifecycle_callbacks():
                        await generation.stop_services(scopes=frozenset({"home"}))
                except Exception as exc:
                    generation.record.status = "degraded"
                    generation.diagnostics.append(
                        PluginDiagnostic(
                            code="plugin_background_service_stop_failed",
                            phase="background",
                            message=generation.describe_error(exc),
                            plugin_id=generation.candidate.plugin_id,
                            generation_id=generation.record.generation_id,
                            suggested_action="Resolve service cleanup before ownership transfer.",
                        )
                    )
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("home-scoped plugin services have not stopped", errors)

    async def synchronize(self) -> None:
        """Apply shared intent at an admission boundary without polling each plugin."""

        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        stamp = await run_blocking(self.store.state_stamp)
        if stamp == self._observed_state_stamp:
            return
        async with self._mutation_lock:
            if self._closed:
                raise RuntimeError("plugin runtime is closed")
            stamp = await run_blocking(self.store.state_stamp)
            if stamp == self._observed_state_stamp:
                return
            state = await run_blocking(self.store.read_state)
            plan = plan_desired_plugin_activation(state, builtin_contracts=self.builtin_contracts)
            eligible: set[str] = set()
            changed: set[str] = set()
            for node in plan.graph.nodes:
                if self._candidate_readiness(state, node.plugin_id) != "ready":
                    continue
                if not set(node.dependencies) <= eligible:
                    continue
                eligible.add(node.plugin_id)
                candidate = self._runtime_candidate(state, node.plugin_id)
                active = self._active.get(node.plugin_id)
                if active is None or (
                    active.candidate.release_id != candidate.release_id
                    or active.candidate.configuration != candidate.configuration
                ):
                    changed.add(node.plugin_id)
            owned_ids = {*self._active, *(g.candidate.plugin_id for g in self._retired.values())}
            removed = owned_ids - eligible
            if removed:
                await self._transition_state_locked(
                    expected=state,
                    candidate_state=state,
                    remove_ids=removed,
                    close_gate_ids=removed,
                    reason="disable",
                )
            rebuild = (
                desired_dependant_closure(state, changed, builtin_contracts=self.builtin_contracts)
                & eligible
            )
            waiting_cleanup = {
                plugin_id for plugin_id in rebuild if self._cleanup_status(plugin_id) is not None
            }
            rebuild -= desired_dependant_closure(
                state, waiting_cleanup, builtin_contracts=self.builtin_contracts
            )
            while rebuild:
                try:
                    await self._transition_state_locked(
                        expected=state,
                        candidate_state=state,
                        rebuild_ids=rebuild,
                        reason="restart",
                    )
                except PluginCandidateBuildError as exc:
                    self._record_failure(
                        self._runtime_candidate(state, exc.plugin_id), "synchronize", exc.cause
                    )
                    rebuild -= desired_dependant_closure(
                        state, {exc.plugin_id}, builtin_contracts=self.builtin_contracts
                    )
                else:
                    break
            # Keep the stamp from before the read. A concurrent replacement is
            # observed at the next boundary, even when it races with publication.
            if not any(self._cleanup_status(item) == "stopping" for item in waiting_cleanup):
                self._observed_state_stamp = stamp

    async def activate(self, plugin_id: str) -> PluginStatus:
        async with self._mutation_lock:
            self._ensure_catalog()
            expected = self.store.read_state()
            candidate = self._runtime_candidate(expected, plugin_id)
            if self._cleanup_status(plugin_id) == "failed":
                return "failed"
            status = self.status(plugin_id, state=expected)
            if plugin_id in self._active:
                status = "ready"
            if status == "failed" and self._failed.get(plugin_id) == candidate.release_id:
                self._failed.pop(plugin_id, None)
                status = self.status(plugin_id, state=expected)
            if status not in {"ready", "active"}:
                return status
            try:
                affected = desired_dependant_closure(
                    expected,
                    {plugin_id},
                    builtin_contracts=self.builtin_contracts,
                )
                affected_graph = self._resolve_dependencies(expected, roots=affected)
                rebuild_ids = {
                    node.plugin_id
                    for node in affected_graph.nodes
                    if node.plugin_id in affected or node.plugin_id not in self._active
                }
                await self._transition_state_locked(
                    expected=expected,
                    candidate_state=expected,
                    rebuild_ids=rebuild_ids,
                    reason="restart" if plugin_id in self._active else "activate",
                )
            except PluginCandidateBuildError as exc:
                failed = self._runtime_candidate(expected, exc.plugin_id)
                self._record_failure(failed, "activation", exc.cause)
                return self.status(plugin_id)
            except Exception as exc:
                self._record_failure(candidate, "activation", exc)
                return "failed"
            return self.status(plugin_id)

    async def disable(self, plugin_id: str, *, wait: bool = True) -> PluginStatus:
        async with self._mutation_lock:
            expected = self.store.read_state()
            candidate_definition = self._runtime_candidate(expected, plugin_id)
            dependents = active_dependents(
                expected,
                plugin_id,
                builtin_contracts=self.builtin_contracts,
            )
            if dependents:
                raise PluginDependencyError(
                    f"plugin has active dependents: {plugin_id}: {', '.join(dependents)}"
                )
            candidate = expected.model_copy(deep=True)
            self._set_desired_enabled(candidate, plugin_id, False)
            await self._transition_state_locked(
                expected=expected,
                candidate_state=candidate,
                remove_ids={plugin_id},
                close_gate_ids={plugin_id},
                reason="disable",
            )
            self.store.append_audit(
                PluginAuditEvent(
                    action="disable",
                    plugin_id=plugin_id,
                    package_digest=candidate_definition.package_digest,
                    metadata={
                        "plugin_class": candidate_definition.plugin_class,
                        "release_id": candidate_definition.release_id,
                    },
                )
            )
            for generation in tuple(self._retired.values()):
                if generation.candidate.plugin_id == plugin_id:
                    generation.revoke(cancel=True)
                    if generation.cleanup is None or generation.cleanup.done():
                        generation.cleanup_error = None
                        self._schedule_retirement(generation, reason="disable")
        return await self.wait_stopped(plugin_id) if wait else self.status(plugin_id)

    async def wait_stopped(self, plugin_id: str) -> PluginStatus:
        """Wait within the local shutdown budget without abandoning cleanup."""

        tasks = [
            generation.cleanup
            for generation in self._retired.values()
            if generation.candidate.plugin_id == plugin_id and generation.cleanup is not None
        ]
        if tasks:
            await asyncio.wait(tasks, timeout=self.stop_timeout_seconds)
        return self.status(plugin_id)

    def _cleanup_status(self, plugin_id: str) -> PluginStatus | None:
        generations = [
            generation
            for generation in self._retired.values()
            if generation.candidate.plugin_id == plugin_id
        ]
        if any(generation.cleanup_error is not None for generation in generations):
            return "failed"
        if any(not generation.gate.is_open for generation in generations):
            return "stopping"
        return None

    async def enable(self, plugin_id: str) -> PluginStatus:
        async with self._mutation_lock:
            if self._closed:
                raise RuntimeError("plugin runtime is closed")
            cleanup_status = self._cleanup_status(plugin_id)
            if cleanup_status is not None:
                return cleanup_status
            expected = self.store.read_state()
            root_candidate = self._runtime_candidate(expected, plugin_id)
            affected = desired_dependant_closure(
                expected,
                {plugin_id},
                builtin_contracts=self.builtin_contracts,
            )
            if self._desired_enabled(expected, plugin_id) and all(
                affected_id in self._active for affected_id in affected
            ):
                return self.status(plugin_id)
            candidate = expected.model_copy(deep=True)
            self._set_desired_enabled(candidate, plugin_id, True)
            try:
                affected = desired_dependant_closure(
                    candidate,
                    {plugin_id},
                    builtin_contracts=self.builtin_contracts,
                )
                graph = self._resolve_dependencies(candidate, roots=affected)
            except PluginDependencyError:
                self.store.commit_candidate_state(expected, candidate)
                return self.status(plugin_id)
            for node in graph.nodes:
                self._set_desired_enabled(candidate, node.plugin_id, True)
            try:
                committed = self.store.commit_candidate_state(expected, candidate)
                not_ready = [
                    (node.plugin_id, self._candidate_readiness(committed, node.plugin_id))
                    for node in graph.nodes
                    if self._candidate_readiness(committed, node.plugin_id) != "ready"
                ]
                if not_ready:
                    target_status = self._candidate_readiness(committed, plugin_id)
                    if target_status != "ready":
                        return target_status
                    return self.status(plugin_id)
                rebuild_ids = {
                    node.plugin_id for node in graph.nodes if node.plugin_id not in self._active
                }
                await self._transition_state_locked(
                    expected=committed,
                    candidate_state=committed,
                    rebuild_ids=rebuild_ids,
                    reason="activate",
                )
            except PluginCandidateBuildError as exc:
                failed = self._runtime_candidate(committed, exc.plugin_id)
                self._record_failure(failed, "activation", exc.cause)
                return self.status(plugin_id)
            except Exception as exc:
                self._record_failure(root_candidate, "activation", exc)
                return self.status(plugin_id)
            return self.status(plugin_id)

    async def restart(self, plugin_id: str) -> PluginStatus:
        state = self.store.read_state()
        self._runtime_candidate(state, plugin_id)
        if not self._desired_enabled(state, plugin_id):
            return "disabled"
        return await self.activate(plugin_id)

    async def apply_candidate_state(
        self,
        *,
        expected: PluginStateDocument,
        candidate: PluginStateDocument,
        roots: set[str],
        reason: str,
        cascade_dependants: bool = True,
        remove_ids: set[str] | None = None,
        close_gate_ids: set[str] | None = None,
    ) -> None:
        """Build and publish a state-changing activation candidate transactionally."""

        async with self._mutation_lock:
            rebuild_ids = set(roots)
            if cascade_dependants:
                rebuild_ids = desired_dependant_closure(
                    candidate,
                    rebuild_ids,
                    builtin_contracts=self.builtin_contracts,
                )
            rebuild_ids = {
                plugin_id
                for plugin_id in rebuild_ids
                if self._desired_enabled(candidate, plugin_id)
            }
            try:
                await self._transition_state_locked(
                    expected=expected,
                    candidate_state=candidate,
                    rebuild_ids=rebuild_ids,
                    remove_ids=remove_ids or set(),
                    close_gate_ids=close_gate_ids or set(),
                    reason=reason,
                )
            except Exception as exc:
                failure_id = exc.plugin_id if isinstance(exc, PluginCandidateBuildError) else None
                root = failure_id or next(iter(sorted(roots)))
                failed = self._runtime_candidate(candidate, root)
                cause = exc.cause if isinstance(exc, PluginCandidateBuildError) else exc
                self._record_failure(failed, reason, cause)
                raise

    def bind_task_service(self, service: TaskServicePort) -> None:
        if self._active or self._started:
            raise RuntimeError("Bind the task service before starting plugins")
        self._task_service = service

    def bind_model_generation(self, service: ModelGenerationPort) -> None:
        if self._active or self._started:
            raise RuntimeError("Bind model generation before starting plugins")
        self._model_generation = service

    async def bind_task(self, target: PluginTaskTarget) -> PluginTaskBinding:
        # Capture and acquire without yielding. Lifecycle callbacks may create
        # schedules after publication while the mutation owner is still active.
        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        generation = self._active.get(target.plugin_id)
        if generation is None:
            raise ValueError(f"Plugin task provider is unavailable: {target.plugin_id}")
        return bind_plugin_task(generation, target)

    def task_owner_available(self, owner_id: str) -> bool:
        generation = self._active.get(owner_id)
        return not self._closed and generation is not None and generation.gate.is_open

    def require_task_owner(self, owner_id: str, target: AgentTaskTarget | PluginTaskTarget) -> None:
        if not self.task_owner_available(owner_id):
            raise ValueError(f"Task owner is unavailable: {owner_id}")
        manifest = self._active[owner_id].candidate.manifest
        permissions = {item.id for item in manifest.permissions}
        required = (
            {"background.run", "sessions.write"}
            if isinstance(target, AgentTaskTarget)
            else {"background.run"}
        )
        if not required.issubset(permissions):
            raise PermissionError(f"Task owner requires permissions: {', '.join(sorted(required))}")
        if isinstance(target, PluginTaskTarget) and target.plugin_id not in {
            owner_id,
            *(item.plugin_id for item in manifest.dependencies.plugins),
        }:
            raise PermissionError(
                "Scheduled service must belong to the task owner or its declared dependency"
            )

    def snapshot(self) -> ExtensionCatalogLease:
        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        self._ensure_catalog()
        with self._catalog_pointer_lock:
            assert self._current_catalog is not None
            return self._current_catalog.acquire()

    def catalog(self) -> ExtensionCatalog:
        """Read the current directory; execution must acquire its own binding."""

        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        self._ensure_catalog()
        with self._catalog_pointer_lock:
            catalog = self._current_catalog
            assert catalog is not None
            return catalog

    @asynccontextmanager
    async def bind_driver(
        self, expected: RunDriverBinding
    ) -> AsyncIterator[ContributionBinding[RunDriverRegistration]]:
        """Own the exact Driver implementation only while it executes accepted work."""

        await self.synchronize()
        generations: tuple[PluginGeneration, ...] = ()
        async with self._mutation_lock:
            try:
                binding = self.catalog().bind_driver(expected.driver_id, expected=expected)
            except ValueError:
                binding, generations = await self._load_driver_contract(expected)
        try:
            yield binding
        finally:
            binding.close()
            cleanup = [
                item.cleanup
                for item in generations
                if item.retiring and item.lease_count == 0 and item.cleanup is not None
            ]
            if cleanup:
                await asyncio.wait(cleanup, timeout=self.stop_timeout_seconds)

    def select_driver(self, driver_id: str) -> RunDriverBinding:
        """Capture a durable implementation identity when new work is accepted."""

        if driver_id == DEFAULT_DRIVER_ID:
            return RunDriverBinding()
        return self.catalog().driver_binding(driver_id)

    async def _load_driver_contract(
        self, expected: RunDriverBinding
    ) -> tuple[ContributionBinding[RunDriverRegistration], tuple[PluginGeneration, ...]]:
        current = self.store.read_state()
        state, graph = resolve_driver_contract(expected, current, self.builtin_contracts)
        implementations = {item.plugin_id: item for item in expected.implementations}
        generations: dict[str, PluginGeneration] = {}
        created: list[PluginGeneration] = []
        leases: list[GenerationLease] = []
        available: dict[str, list[PluginGeneration]] = {}
        for item in (*self._active.values(), *self._retired.values()):
            available.setdefault(item.record.plugin_id, []).append(item)
        try:
            for node in graph.nodes:
                implementation = implementations[node.plugin_id]
                generation = next(
                    (
                        item
                        for item in available.get(node.plugin_id, ())
                        if item.gate.is_open
                        and (not item.retiring or item.lease_count > 0)
                        and plugin_implementation(item.record) == implementation
                        and all(
                            dependency is generations[dependency.record.plugin_id]
                            for dependency in item.dependencies
                        )
                    ),
                    None,
                )
                if generation is None:
                    # A retirement already admitted at zero leases cannot be
                    # reversed by acquiring another reference to that instance.
                    stopping = [
                        item.cleanup
                        for item in self._retired.values()
                        if item.record.plugin_id == node.plugin_id
                        and (not item.gate.is_open or item.lease_count == 0)
                        and item.cleanup is not None
                    ]
                    if stopping:
                        await asyncio.wait(stopping, timeout=self.stop_timeout_seconds)
                    if self._cleanup_status(node.plugin_id) is not None:
                        raise PluginGenerationStartError(
                            f"plugin resources have not stopped: {node.plugin_id}"
                        )
                    readiness = self._candidate_readiness(state, node.plugin_id)
                    if readiness != "ready":
                        raise PluginGenerationStartError(
                            f"Run driver implementation is not ready: {node.plugin_id}: {readiness}"
                        )
                    dependency_graph = self._resolve_dependencies(state, roots={node.plugin_id})
                    fingerprint = build_dependency_lock_fingerprint(
                        dependency_graph,
                        state,
                        {key: item.record for key, item in generations.items()},
                        owner_plugin_id=node.plugin_id,
                    )
                    generation = await self._build_generation(
                        self._runtime_candidate(state, node.plugin_id),
                        dependency_lock_fingerprint=fingerprint,
                        dependencies={key: generations[key] for key in node.dependencies},
                    )
                    created.append(generation)
                    if plugin_implementation(generation.record) != implementation:
                        raise ValueError(
                            f"Run driver execution contract is unavailable: {node.plugin_id}"
                        )
                generations[node.plugin_id] = generation
                leases.append(generation.acquire())

            assert expected.implementation is not None
            owner = generations[expected.implementation.plugin_id]
            prepared = bind_generation_gate(owner.contributions, owner.gate)
            driver = next(
                (item for item in prepared.run_drivers if item.canonical_id == expected.driver_id),
                None,
            )
            if driver is None or driver.contract_hash != expected.contract_hash:
                raise ValueError(f"Run driver contract is unavailable: {expected.driver_id}")
            driver = replace(
                driver, implementation=expected.implementation, dependencies=expected.dependencies
            )
            if self._closed or self.store.read_state() != current:
                raise PluginStateConflictError("plugin state changed while loading a Run driver")
            created_ids = {item.record.generation_id for item in created}
            if any(
                item.record.status not in {"active", "degraded"}
                or (not item.gate.is_open and item.record.generation_id not in created_ids)
                for item in generations.values()
            ):
                raise PluginGenerationStartError("Run driver implementation stopped while loading")
            for generation in created:
                if generation.data_store is not None:
                    generation.data_store.activate()
                generation.tasks.activate()
                generation.gate.open()
                # These implementations serve existing Runs. Their background
                # services and global contributions remain unpublished.
                self._schedule_retirement(generation, reason="restart")
            return ContributionBinding(driver, tuple(leases)), tuple(generations.values())
        except BaseException:
            for lease in reversed(leases):
                lease.close()
            await self._discard_candidates(tuple(reversed(created)))
            raise

    async def replace_base_skills(self, skills: tuple[SkillMetadata, ...]) -> None:
        """Publish a new Host Skill baseline for future Catalog leases.

        Existing leases retain their immutable Catalog. The candidate is fully
        projected with the active plugin generations before the pointer moves,
        so a Host/plugin Skill conflict cannot partially publish.
        """

        async with self._mutation_lock:
            if self._closed:
                raise RuntimeError("plugin runtime is closed")
            if skills == self.base_skills:
                return
            candidate_skills = tuple(skill.model_copy(deep=True) for skill in skills)
            current = self._ensure_catalog()
            catalog_base = self._capture_catalog_base(skills=candidate_skills)
            catalog = current.rebase(catalog_base)
            self._publish_catalog(
                catalog,
                catalog_base=catalog_base,
                base_skills=candidate_skills,
            )

    def project_status(
        self, record: PluginRecord, *, state: PluginStateDocument | None = None
    ) -> PluginStatus:
        installed = record.selected()
        if installed is None:
            return "invalid"
        active = self._active.get(record.plugin_id)
        if active is not None:
            if isinstance(active.instance, WorkerPluginInstance) and not active.instance.is_healthy:
                active.gate.close()
                self._schedule_generation_failure(active)
            return active.record.status
        cleanup_status = self._cleanup_status(record.plugin_id)
        if cleanup_status is not None:
            return cleanup_status
        if self._failed.get(record.plugin_id) == installed.package_digest:
            return "failed"
        if not record.activation.desired_enabled:
            return "disabled"
        package_root = self.store.package_path(record.plugin_id, installed.package_digest)
        readiness = plugin_install_readiness(installed, record.activation, package_root)
        if readiness != "ready":
            return readiness
        if state is None:
            state = self.store.read_state()
        if self._dependency_resolution_error(state, record.plugin_id) is not None:
            return "incompatible"
        if self._dependency_blockers(state, record.plugin_id):
            return "blocked"
        return "ready"

    def project_builtin_status(
        self, plugin_id: str, *, state: PluginStateDocument | None = None
    ) -> PluginStatus:
        definition = self.builtin_plugins.require(plugin_id)
        active = self._active.get(plugin_id)
        if active is not None:
            return active.record.status
        cleanup_status = self._cleanup_status(plugin_id)
        if cleanup_status is not None:
            return cleanup_status
        if self._failed.get(plugin_id) == definition.contract_hash:
            return "failed"
        if state is None:
            state = self.store.read_state()
        if not self._desired_enabled(state, plugin_id):
            return "disabled"
        error = self._dependency_resolution_error(state, plugin_id)
        if error is not None:
            return "incompatible"
        if self._dependency_blockers(state, plugin_id):
            return "blocked"
        return "ready"

    def status(self, plugin_id: str, *, state: PluginStateDocument | None = None) -> PluginStatus:
        if state is None:
            state = self.store.read_state()
        if self.builtin_plugins.get(plugin_id) is not None:
            return self.project_builtin_status(plugin_id, state=state)
        return self.project_status(_state_plugin_record(state, plugin_id), state=state)

    def has_resources(self, plugin_id: str, *, package_digest: str | None = None) -> bool:
        """Include retired and failed owners when deciding whether files may be removed."""

        return any(
            generation.candidate.plugin_id == plugin_id
            and (package_digest is None or generation.candidate.package_digest == package_digest)
            for generations in (self._active.values(), self._retired.values())
            for generation in generations
        )

    def diagnostics(self, plugin_id: str | None = None) -> list[PluginDiagnostic]:
        values = [
            *self._diagnostics,
            *(
                diagnostic
                for generation in [*self._active.values(), *self._retired.values()]
                for diagnostic in generation.diagnostics
            ),
        ]
        state = self.store.read_state()
        observed_ids = (
            [plugin_id]
            if plugin_id is not None
            else sorted({*state.plugins, *self.builtin_plugins.plugin_ids})
        )
        for observed_id in observed_ids:
            diagnostic = self._dependency_diagnostic(state, observed_id)
            if diagnostic is not None:
                values.append(diagnostic)
        if plugin_id is not None:
            values = [item for item in values if item.plugin_id == plugin_id]
        unique: dict[tuple[str, str | None, str], PluginDiagnostic] = {}
        for item in values:
            unique[(item.code, item.plugin_id, item.message)] = item
        return list(unique.values())

    async def close(self) -> None:
        if self._close_task is not None and self._close_task.done():
            if self._close_task.cancelled() or self._close_task.exception() is not None:
                self._close_task = None
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close(), name="ouro-plugin-shutdown")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        async with self._mutation_lock:
            catalog = self._ensure_catalog()
            generations = [
                *(self._active[record.plugin_id] for record in catalog.generations),
                *self._retired.values(),
            ]
            for generation in generations:
                generation.revoke(cancel=True)
                generation.retiring = True
                self._retired[generation.record.generation_id] = generation
            self._active.clear()
            self._publish_catalog(self._build_catalog({}))

        errors: list[Exception] = []
        seen: set[str] = set()
        for generation in reversed(generations):
            if generation.record.generation_id in seen:
                continue
            seen.add(generation.record.generation_id)
            pending = generation.cleanup
            if pending is None or pending.done():
                generation.cleanup_error = None
                self._schedule_retirement(generation, reason="shutdown")
                pending = generation.cleanup
            assert pending is not None
            await asyncio.wait((pending,), timeout=self.stop_timeout_seconds)
            if not pending.done():
                errors.append(
                    TimeoutError(
                        f"plugin cleanup is still running: {generation.record.generation_id}"
                    )
                )
            elif generation.cleanup_error is not None:
                errors.append(generation.cleanup_error)
        if errors:
            raise ExceptionGroup("plugin runtime shutdown failed", errors)

    async def _transition_state_locked(
        self,
        *,
        expected: PluginStateDocument,
        candidate_state: PluginStateDocument,
        rebuild_ids: set[str] | None = None,
        remove_ids: set[str] | None = None,
        close_gate_ids: set[str] | None = None,
        startup_graph: PluginLockGraph | None = None,
        reason: str,
    ) -> None:
        """Prepare a complete candidate before committing state and catalog together."""

        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        self._ensure_catalog()
        if self.store.read_state() != expected:
            raise PluginStateConflictError(
                "plugin state changed before the activation candidate could build"
            )
        candidate_state = PluginStateDocument.model_validate(
            candidate_state.model_dump(mode="python")
        )
        requested_rebuilds = set(rebuild_ids or ())
        requested_removals = set(remove_ids or ())
        gated_removals = set(close_gate_ids or ())
        known_ids = {
            *expected.plugins,
            *self.builtin_plugins.plugin_ids,
            *self._active,
            *(generation.candidate.plugin_id for generation in self._retired.values()),
        }
        unknown = (requested_rebuilds | requested_removals | gated_removals) - known_ids
        if unknown:
            raise KeyError(f"plugin is not available: {sorted(unknown)[0]}")
        requested_rebuilds -= requested_removals

        graph = startup_graph or (
            self._resolve_dependencies(candidate_state, roots=requested_rebuilds)
            if requested_rebuilds
            else None
        )
        build_ids = set(requested_rebuilds)
        if graph is not None:
            for node in graph.nodes:
                active = self._active.get(node.plugin_id)
                release_id = node.package_digest or node.contract_hash
                if active is None or active.candidate.release_id != release_id:
                    build_ids.add(node.plugin_id)

        candidates: dict[str, PluginGeneration] = {}
        graph_nodes = {node.plugin_id: node for node in graph.nodes} if graph is not None else {}
        published = False
        try:
            if graph is not None:
                for node in graph.nodes:
                    if node.plugin_id not in build_ids:
                        continue
                    if startup_graph is not None and any(
                        dependency in build_ids and dependency not in candidates
                        for dependency in node.dependencies
                    ):
                        continue
                    try:
                        candidates[node.plugin_id] = await self._prepare_generation(
                            candidate_state, node, graph_nodes, candidates, requested_removals
                        )
                    except Exception as exc:
                        if startup_graph is None:
                            raise PluginCandidateBuildError(node.plugin_id, exc) from exc
                        self._record_failure(
                            self._runtime_candidate(candidate_state, node.plugin_id),
                            "startup",
                            exc,
                        )

            next_generations = {
                plugin_id: generation
                for plugin_id, generation in self._active.items()
                if plugin_id not in requested_removals and plugin_id not in build_ids
            }
            next_generations.update(candidates)
            while True:
                try:
                    catalog = self._build_catalog(
                        next_generations, state=candidate_state, graph=startup_graph
                    )
                    break
                except CatalogContributionError as exc:
                    if startup_graph is None or exc.plugin_id not in candidates:
                        raise
                    self._record_failure(candidates[exc.plugin_id].candidate, "startup", exc)
                    rejected = {exc.plugin_id}
                    for node in startup_graph.nodes:
                        if rejected.intersection(node.dependencies):
                            rejected.add(node.plugin_id)
                    discarded = tuple(
                        candidates.pop(key)
                        for key in reversed(tuple(candidates))
                        if key in rejected
                    )
                    for key in rejected:
                        next_generations.pop(key, None)
                    await self._discard_candidates(discarded)
            if self._closed:
                raise RuntimeError("plugin runtime closed during candidate preparation")
            self.store.commit_candidate_state(expected, candidate_state)

            previous = {
                plugin_id: generation
                for plugin_id, generation in self._active.items()
                if plugin_id in requested_removals or plugin_id in build_ids
            }
            owned = (*self._active.values(), *self._retired.values())
            revoke_dependent_generations(
                owned,
                {
                    item.record.generation_id
                    for item in owned
                    if item.record.plugin_id in gated_removals
                },
            )
            for generation in candidates.values():
                if generation.data_store is not None:
                    generation.data_store.activate()
                generation.tasks.activate()
                generation.gate.open()
            self._active = next_generations
            for plugin_id in candidates.keys() | requested_removals:
                self._failed.pop(plugin_id, None)
            self._publish_catalog(catalog)
            published = True
            for generation in previous.values():
                generation.retiring = True
                self._retired[generation.record.generation_id] = generation

            handover = asyncio.create_task(
                self._finish_publication(previous, candidates, graph=graph, reason=reason),
                name="ouro-plugin-publication",
            )
            try:
                await asyncio.shield(handover)
            except asyncio.CancelledError:
                await settle_owned_tasks(handover)
                handover.result()
                raise
        except BaseException:
            if not published:
                await self._discard_candidates(tuple(reversed(tuple(candidates.values()))))
            raise

    async def _prepare_generation(
        self,
        state: PluginStateDocument,
        node: LockedPluginDependency,
        graph_nodes: dict[str, LockedPluginDependency],
        candidates: dict[str, PluginGeneration],
        removals: set[str],
    ) -> PluginGeneration:
        if self._cleanup_status(node.plugin_id) is not None:
            raise PluginGenerationStartError(f"plugin resources have not stopped: {node.plugin_id}")
        readiness = self._candidate_readiness(state, node.plugin_id)
        if readiness != "ready":
            raise PluginGenerationStartError(
                f"plugin candidate is not activation-ready: {node.plugin_id}: {readiness}"
            )
        dependencies: dict[str, PluginGeneration] = {}
        for dependency_id in node.dependencies:
            dependency_node = graph_nodes[dependency_id]
            dependency = candidates.get(dependency_id) or self._active.get(dependency_id)
            if dependency is None or dependency_id in removals:
                raise PluginDependencyError(
                    f"required plugin generation is unavailable: {dependency_id}"
                )
            expected_release_id = dependency_node.package_digest or dependency_node.contract_hash
            if dependency.candidate.release_id != expected_release_id:
                raise PluginDependencyError(f"required plugin generation is stale: {dependency_id}")
            dependencies[dependency_id] = dependency
        # Resolve versions once per operation. Each fingerprint still includes
        # the exact transitive contracts, without rewalking manifests or state.
        dependency_nodes: dict[str, LockedPluginDependency] = {}
        records: dict[str, PluginGenerationRecord] = {}
        pending = list(dependencies.values())
        while pending:
            generation = pending.pop()
            plugin_id = generation.record.plugin_id
            if plugin_id in dependency_nodes:
                continue
            dependency_nodes[plugin_id] = graph_nodes[plugin_id]
            records[plugin_id] = generation.record
            pending.extend(generation.dependencies)
        fingerprint = build_dependency_lock_fingerprint(
            PluginLockGraph(tuple(dependency_nodes.values())),
            state,
            records,
            owner_plugin_id=node.plugin_id,
        )
        return await self._build_generation(
            self._runtime_candidate(state, node.plugin_id),
            dependency_lock_fingerprint=fingerprint,
            dependencies=dependencies,
        )

    async def _discard_candidates(self, candidates: tuple[PluginGeneration, ...]) -> None:
        cleanup = []
        for generation in candidates:
            generation.revoke(cancel=True)
            self._schedule_retirement(generation, reason="startup_failed")
            assert generation.cleanup is not None
            cleanup.append(generation.cleanup)
        if cleanup:
            await asyncio.wait(cleanup, timeout=self.stop_timeout_seconds)

    async def _finish_publication(
        self,
        previous: dict[str, PluginGeneration],
        candidates: dict[str, PluginGeneration],
        *,
        graph: PluginLockGraph | None,
        reason: str,
    ) -> None:
        if candidates:
            await self._handover_background_services(previous, candidates, graph=graph)
        retirement_order = (
            graph.shutdown_order if graph is not None else tuple(sorted(previous, reverse=True))
        )
        scheduled: set[str] = set()
        for plugin_id in retirement_order:
            generation = previous.get(plugin_id)
            if generation is None:
                continue
            scheduled.add(plugin_id)
            self._schedule_retirement(
                generation,
                reason=_retirement_reason(
                    reason,
                    replaced=plugin_id in candidates,
                    old=generation,
                    new=candidates.get(plugin_id),
                ),
            )
        for plugin_id in sorted(set(previous) - scheduled, reverse=True):
            generation = previous[plugin_id]
            self._schedule_retirement(
                generation,
                reason=_retirement_reason(
                    reason,
                    replaced=plugin_id in candidates,
                    old=generation,
                    new=candidates.get(plugin_id),
                ),
            )

    def _candidate_readiness(
        self,
        state: PluginStateDocument,
        plugin_id: str,
    ) -> PluginStatus:
        if not self._desired_enabled(state, plugin_id):
            return "disabled"
        if self.builtin_plugins.get(plugin_id) is not None:
            return "ready"
        record = state.plugins.get(plugin_id)
        if record is None:
            return "invalid"
        installed = record.selected()
        if installed is None:
            return "invalid"
        package_root = self.store.package_path(record.plugin_id, installed.package_digest)
        return plugin_install_readiness(installed, record.activation, package_root)

    def _dependency_resolution_error(
        self,
        state: PluginStateDocument,
        plugin_id: str,
    ) -> PluginDependencyError | None:
        try:
            self._resolve_dependencies(state, roots={plugin_id})
        except PluginDependencyError as exc:
            return exc
        return None

    def _dependency_blockers(
        self,
        state: PluginStateDocument,
        plugin_id: str,
    ) -> list[tuple[str, PluginStatus]]:
        try:
            graph = self._resolve_dependencies(state, roots={plugin_id})
        except PluginDependencyError:
            return []
        blockers: list[tuple[str, PluginStatus]] = []
        for node in graph.nodes:
            if node.plugin_id == plugin_id:
                continue
            active = self._active.get(node.plugin_id)
            if active is not None and active.record.status in {"active", "degraded"}:
                continue
            release_id = node.package_digest or node.contract_hash
            if self._failed.get(node.plugin_id) == release_id:
                status: PluginStatus = "failed"
            else:
                status = self._candidate_readiness(state, node.plugin_id)
            blockers.append((node.plugin_id, status))
        return blockers

    def _resolve_dependencies(
        self,
        state: PluginStateDocument,
        *,
        roots: set[str] | None = None,
    ) -> PluginLockGraph:
        return resolve_plugin_dependencies(
            state,
            roots=roots,
            builtin_contracts=self.builtin_contracts,
        )

    def _dependency_diagnostic(
        self,
        state: PluginStateDocument,
        plugin_id: str,
    ) -> PluginDiagnostic | None:
        if not self._plugin_available(state, plugin_id) or not self._desired_enabled(
            state, plugin_id
        ):
            return None
        error = self._dependency_resolution_error(state, plugin_id)
        if error is not None:
            return PluginDiagnostic(
                code="plugin_dependency_resolution_failed",
                phase="dependencies",
                message=redact_text(str(error)),
                plugin_id=plugin_id,
                suggested_action="Install and select compatible plugin dependencies.",
            )
        blockers = self._dependency_blockers(state, plugin_id)
        if not blockers:
            return None
        detail = ", ".join(f"{item_id} ({status})" for item_id, status in blockers)
        return PluginDiagnostic(
            code="plugin_dependency_blocked",
            phase="dependencies",
            message=f"plugin dependencies are not active: {detail}",
            plugin_id=plugin_id,
            suggested_action="Repair or retry the blocked dependency.",
        )

    def _ensure_catalog(self) -> ExtensionCatalog:
        with self._catalog_pointer_lock:
            current = self._current_catalog
            # Background MCP discovery mutates the Host registry after plugin
            # startup. New leases follow that baseline; existing leases keep the
            # previously published immutable Catalog.
            if (
                current is not None
                and self._catalog_base is not None
                and self._catalog_base.tool_registry_revision == self.coordinator.tools.revision
            ):
                return current
            catalog_base = self._capture_catalog_base(skills=self.base_skills)
            catalog = (
                build_extension_catalog(catalog_base, ())
                if current is None
                else current.rebase(catalog_base)
            )
            self._catalog_base = catalog_base
            self._current_catalog = catalog
            return catalog

    def _capture_catalog_base(
        self,
        *,
        skills: tuple[SkillMetadata, ...],
    ) -> ExtensionCatalogBase:
        return ExtensionCatalogBase.capture(
            tools=self.coordinator.tools,
            observers=self.coordinator.observers,
            context_providers=self.coordinator.context_providers,
            commands=self.coordinator.commands,
            compaction=self.coordinator.compaction,
            continuations=self.coordinator.continuations,
            prompt_policies=self.coordinator.prompt_policies,
            turn_policies=self.coordinator.turn_policies,
            tool_policies=self.coordinator.tool_policies,
            skills=skills,
        )

    def _build_catalog(
        self,
        generations: dict[str, PluginGeneration],
        *,
        state: PluginStateDocument | None = None,
        graph: PluginLockGraph | None = None,
    ) -> ExtensionCatalog:
        self._ensure_catalog()
        assert self._catalog_base is not None
        if not generations:
            ordered_ids: tuple[str, ...] = ()
        elif graph is not None:
            ordered_ids = tuple(key for key in graph.activation_order if key in generations)
        elif state is None:
            raise ValueError("plugin state is required to order active generations")
        else:
            graph = self._resolve_dependencies(state, roots=set(generations))
            ordered_ids = graph.activation_order
            if set(ordered_ids) != set(generations):
                raise PluginDependencyError(
                    "active plugin generations do not match the resolved dependency graph"
                )
        return build_extension_catalog(
            self._catalog_base,
            tuple(generations[plugin_id] for plugin_id in ordered_ids),
            tool_groups=tuple(
                group
                for plugin_id in ordered_ids
                if (endpoint := generations[plugin_id].tool_groups) is not None
                for group in endpoint.versions
            ),
            dependencies={
                plugin_id: tuple(
                    item.plugin_id for item in generation.candidate.manifest.dependencies.plugins
                )
                for plugin_id, generation in generations.items()
            },
        )

    async def _publish_tool_group(
        self,
        endpoint: PluginToolGroups,
        group_id: str,
        version: ToolGroupVersion | None,
        expected: ToolGroupVersion | None,
    ) -> None:
        # A lifecycle mutation may be draining the calling callback. Never wait
        # for that same mutation from inside plugin code.
        if self._mutation_lock.locked():
            raise RuntimeError("tool groups cannot change during plugin lifecycle mutation")
        async with self._mutation_lock:
            endpoint.require_writable()
            active = self._active.get(endpoint.record.plugin_id)
            if active is None or active.tool_groups is not endpoint:
                raise RuntimeError("only the current generation can publish tool groups")
            if expected is not None and endpoint.get(group_id) is not expected:
                return
            groups = [
                group
                for generation in self._active.values()
                if generation.tool_groups is not None
                for group in generation.tool_groups.versions
                if not (generation.tool_groups is endpoint and group.group_id == group_id)
            ]
            if version is not None:
                groups.append(version)
            current = self._ensure_catalog()
            assert self._catalog_base is not None
            catalog = current.with_tool_groups(self._catalog_base, groups)
            # Construction validates all names and schemas before either state
            # changes. Snapshot readers see one complete catalog under this lock.
            with self._catalog_pointer_lock:
                self._current_catalog = catalog
                endpoint.commit(group_id, version)

    def _publish_catalog(
        self,
        catalog: ExtensionCatalog,
        *,
        catalog_base: ExtensionCatalogBase | None = None,
        base_skills: tuple[SkillMetadata, ...] | None = None,
    ) -> None:
        with self._catalog_pointer_lock:
            if catalog_base is not None:
                self._catalog_base = catalog_base
            if base_skills is not None:
                self.base_skills = base_skills
            self._current_catalog = catalog
        if self._task_service is not None:
            self._task_service.notify_plugin_change()

    def _schedule_retirement(
        self,
        generation: PluginGeneration,
        *,
        reason: StopReason,
    ) -> None:
        generation.retiring = True
        self._retired[generation.record.generation_id] = generation
        if generation.lease_count == 0:
            generation.retirement_ready.set()
        if generation.cleanup is not None and not generation.cleanup.done():
            return
        task = asyncio.create_task(
            self._retire_generation(generation, reason=reason),
            name=f"ouro-plugin-retire-{generation.record.generation_id}",
        )
        generation.cleanup = task
        self._retirement_tasks.add(task)
        task.add_done_callback(self._retirement_tasks.discard)

    def _schedule_generation_failure(self, generation: PluginGeneration) -> None:
        if self._closed or generation.retiring:
            return
        generation.retiring = True
        try:
            task = asyncio.create_task(
                self._handle_generation_failure(generation),
                name=f"ouro-plugin-failure-{generation.record.generation_id}",
            )
        except RuntimeError:
            generation.retiring = False
            return
        self._retirement_tasks.add(task)
        task.add_done_callback(self._retirement_tasks.discard)

    async def _handle_generation_failure(
        self,
        generation: PluginGeneration,
    ) -> None:
        async with self._mutation_lock:
            if self._closed:
                return
            current = self._active.get(generation.candidate.plugin_id)
            if current is not generation:
                return
            expected = self.store.read_state()
            affected = desired_dependant_closure(
                expected,
                {generation.candidate.plugin_id},
                builtin_contracts=self.builtin_contracts,
            )
            try:
                await self._transition_state_locked(
                    expected=expected,
                    candidate_state=expected,
                    remove_ids=affected,
                    close_gate_ids=affected,
                    reason="failure",
                )
            except Exception as exc:  # noqa: BLE001 - gate is already fail-closed.
                generation.record.status = "failed"
                self._diagnostics.append(
                    PluginDiagnostic(
                        code="plugin_failure_reconciliation_failed",
                        phase="reconcile",
                        message=f"{type(exc).__name__}: {redact_text(str(exc))}",
                        plugin_id=generation.candidate.plugin_id,
                        version=generation.candidate.version,
                        plugin_class=generation.candidate.plugin_class,
                        release_id=generation.candidate.release_id,
                        package_digest=generation.candidate.package_digest,
                        builtin_contract_hash=generation.candidate.builtin_contract_hash,
                        generation_id=generation.record.generation_id,
                        suggested_action="Retry plugin restart or restart OURO.",
                    )
                )
                return
            self._failed[generation.candidate.plugin_id] = generation.candidate.release_id
            self.store.append_audit(
                PluginAuditEvent(
                    action="failure",
                    plugin_id=generation.candidate.plugin_id,
                    package_digest=generation.candidate.package_digest,
                    metadata={
                        "plugin_class": generation.candidate.plugin_class,
                        "release_id": generation.candidate.release_id,
                    },
                    result="failed",
                    detail="worker generation exited unexpectedly",
                )
            )

    async def _retire_generation(
        self,
        generation: PluginGeneration,
        *,
        reason: StopReason,
    ) -> None:
        await generation.retirement_ready.wait()
        try:
            if generation.cleanup_error is not None:
                raise generation.cleanup_error
            await self._stop_generation(generation, reason=reason)
        except Exception as exc:  # noqa: BLE001 - retirement cannot corrupt current catalog.
            generation.cleanup_error = exc
            generation.record.status = "failed"
            self._diagnostics.append(
                PluginDiagnostic(
                    code="plugin_retirement_failed",
                    phase="retire",
                    message=generation.describe_error(exc),
                    plugin_id=generation.candidate.plugin_id,
                    version=generation.candidate.version,
                    plugin_class=generation.candidate.plugin_class,
                    release_id=generation.candidate.release_id,
                    package_digest=generation.candidate.package_digest,
                    builtin_contract_hash=generation.candidate.builtin_contract_hash,
                    generation_id=generation.record.generation_id,
                    suggested_action="Resolve the cleanup failure, then disable the plugin again.",
                )
            )

    async def _handover_background_services(
        self,
        previous: dict[str, PluginGeneration],
        candidates: dict[str, PluginGeneration],
        *,
        graph: PluginLockGraph | None,
    ) -> None:
        shutdown_order = (
            graph.shutdown_order if graph is not None else tuple(sorted(previous, reverse=True))
        )
        for plugin_id in shutdown_order:
            generation = previous.get(plugin_id)
            if generation is not None:
                try:
                    with self._admit_lifecycle_callbacks():
                        await generation.stop_services()
                except Exception as exc:  # noqa: BLE001 - catalog publication is authoritative.
                    generation.diagnostics.append(
                        PluginDiagnostic(
                            code="plugin_background_service_stop_failed",
                            phase="background",
                            message=generation.describe_error(exc),
                            plugin_id=generation.candidate.plugin_id,
                            version=generation.candidate.version,
                            plugin_class=generation.candidate.plugin_class,
                            release_id=generation.candidate.release_id,
                            package_digest=generation.candidate.package_digest,
                            builtin_contract_hash=(generation.candidate.builtin_contract_hash),
                            generation_id=generation.record.generation_id,
                            suggested_action="Inspect the service and restart OURO if needed.",
                        )
                    )
        activation_order = (
            graph.activation_order if graph is not None else tuple(sorted(candidates))
        )
        for plugin_id in activation_order:
            generation = candidates.get(plugin_id)
            if generation is not None:
                await self._start_generation_services(generation)

    async def _start_generation_services(
        self,
        generation: PluginGeneration,
        *,
        scopes: frozenset[BackgroundServiceScope] | None = None,
    ) -> None:
        if scopes is None:
            scopes = frozenset({"host", "home"} if self._home_services_owner else {"host"})
        if any(
            old.candidate.plugin_id == generation.candidate.plugin_id
            and any(service.scope in scopes for service in old.started_services)
            for old in self._retired.values()
            if old is not generation
        ):
            generation.record.status = "degraded"
            generation.diagnostics.append(
                PluginDiagnostic(
                    code="plugin_background_service_handover_blocked",
                    phase="background",
                    message="Previous generation has not released its background services",
                    plugin_id=generation.candidate.plugin_id,
                    generation_id=generation.record.generation_id,
                    suggested_action="Resolve plugin cleanup before enabling it.",
                )
            )
            return
        try:
            with self._admit_lifecycle_callbacks():
                await generation.start_services(scopes=scopes)
        except Exception as exc:  # The published contribution catalog remains usable.
            generation.record.status = "degraded"
            generation.diagnostics.append(
                PluginDiagnostic(
                    code="plugin_background_service_start_failed",
                    phase="background",
                    message=generation.describe_error(exc),
                    plugin_id=generation.candidate.plugin_id,
                    generation_id=generation.record.generation_id,
                    suggested_action="Fix the background service and restart the plugin.",
                )
            )
            try:
                with self._admit_lifecycle_callbacks():
                    await generation.stop_services(scopes=scopes)
            except Exception:
                # stop_services retains the failed owners and their cleanup error.
                generation.record.status = "failed"

    async def _build_generation(
        self,
        candidate: PluginRuntimeCandidate,
        *,
        dependency_lock_fingerprint: str,
        dependencies: dict[str, PluginGeneration],
    ) -> PluginGeneration:
        configuration = candidate.configuration
        generation = PluginGenerationRecord(
            generation_id=new_id("plg"),
            plugin_id=candidate.plugin_id,
            version=candidate.version,
            plugin_class=candidate.plugin_class,
            release_id=candidate.release_id,
            package_digest=candidate.package_digest,
            builtin_contract_hash=candidate.builtin_contract_hash,
            runtime_kind=candidate.manifest.runtime.kind,
            configuration_revision_id=configuration.revision_id,
            dependency_lock_fingerprint=dependency_lock_fingerprint,
            plugin_api_version=PLUGIN_API_VERSION,
            worker_protocol_version=(
                candidate.manifest.runtime.protocol
                if candidate.manifest.runtime.kind == "worker"
                else None
            ),
            status="starting",
        )
        generation_gate = GenerationGate(generation.generation_id, initially_open=False)
        diagnostics: list[PluginDiagnostic] = []
        redactor = SensitiveValueRedactor()

        def task_failure(exc: Exception) -> None:
            if generation.status == "active":
                generation.status = "degraded"
            diagnostics.append(
                PluginDiagnostic(
                    code="plugin_background_task_failed",
                    phase="background",
                    message=f"{type(exc).__name__}: {redactor.redact(str(exc))}",
                    plugin_id=candidate.plugin_id,
                    version=candidate.version,
                    plugin_class=candidate.plugin_class,
                    release_id=candidate.release_id,
                    package_digest=candidate.package_digest,
                    builtin_contract_hash=candidate.builtin_contract_hash,
                    generation_id=generation.generation_id,
                )
            )

        tasks = OwnedTaskGroup(task_failure, active=False)
        resources: list[Any] = []
        package_root = (
            self.store.verify_package(candidate.plugin_id, candidate.package_digest)
            if candidate.package_digest is not None
            else candidate.root
        )
        active = PluginGeneration(
            record=generation,
            candidate=candidate,
            instance=_DeclarativePluginInstance(),
            contributions=PreparedContributionSet(
                owner=ContributionOwner(
                    plugin_id=candidate.plugin_id,
                    plugin_class=candidate.plugin_class,
                    release_id=candidate.release_id,
                    generation_id=generation.generation_id,
                    package_digest=candidate.package_digest,
                    builtin_contract_hash=candidate.builtin_contract_hash,
                )
            ),
            gate=generation_gate,
            tasks=tasks,
            redactor=redactor,
            dependencies=tuple(dependencies.values()),
            resources=resources,
            diagnostics=diagnostics,
        )
        active.services = ServiceEndpoint(
            candidate.plugin_id,
            gate=generation_gate,
            bind=active.bind,
            dependencies={
                key: dependency.services
                for key, dependency in dependencies.items()
                if dependency.services is not None
            },
        )
        if candidate.manifest.tool_groups:
            active.tool_groups = PluginToolGroups(
                candidate.manifest,
                generation,
                active.contributions.owner,
                generation_gate,
                self._publish_tool_group,
            )
        processes = PluginProcessService(
            generation_gate,
            frozenset(request.id for request in candidate.manifest.permissions),
        )
        environment = PluginEnvironmentService(self._instruction_loader, generation_gate)
        native_run_ports = PluginNativePorts(
            generation_gate,
            frozenset(request.id for request in candidate.manifest.permissions),
            plugin_id=candidate.plugin_id,
            generation_service=self._model_generation,
        )
        scheduler = (
            NativePluginScheduler(
                self._task_service,
                candidate.plugin_id,
                generation_gate,
                frozenset(request.id for request in candidate.manifest.permissions),
            )
            if self._task_service is not None
            else None
        )
        try:
            active.data_lock = self.store.data_lock(candidate.plugin_id)
            if not active.data_lock.acquire():
                raise RuntimeError(f"plugin data is in use for maintenance: {candidate.plugin_id}")
            credential_slots = BoundCredentialResolver(
                dict(configuration.credential_refs), self.credential_resolver, redactor
            )
            resolved_credentials: dict[str, str] = {}
            specification = candidate.manifest.configuration
            if specification is not None:
                for slot, declaration in specification.credentials.items():
                    if declaration.required:
                        resolved_credentials[slot] = credential_slots.resolve(slot)
            if candidate.manifest.runtime.kind == "python-inprocess":
                if candidate.plugin_class == "builtin":
                    factory = candidate.factory
                    assert factory is not None
                else:
                    entrypoint = candidate.manifest.runtime.entrypoint
                    assert entrypoint is not None
                    factory, active.module = load_plugin_factory(
                        package_root,
                        entrypoint,
                        module_namespace=plugin_module_namespace(candidate.manifest.namespace),
                        generation_id=generation.generation_id,
                    )
                active.data_store = ScopedPluginDataStore(
                    self.store.data_root / candidate.plugin_id,
                    read_only=True,
                )
                context = build_plugin_context(
                    candidate,
                    generation,
                    data_root=self.store.data_root,
                    configuration=configuration.values_dict(),
                    credential_bindings=dict(configuration.credential_refs),
                    credential_resolver=self.credential_resolver,
                    tasks=tasks,
                    diagnostics=diagnostics,
                    redactor=redactor,
                    data_store=active.data_store,
                    artifact_root=self.store.home / "artifacts",
                    environment=environment,
                    services=active.services,
                    processes=processes,
                    models=native_run_ports,
                    tools=native_run_ports,
                    tool_groups=active.tool_groups,
                    scheduler=scheduler,
                )
                instance = factory(context)
                if inspect.isawaitable(instance):
                    instance = await instance
                active.instance = instance
            elif candidate.manifest.runtime.kind == "worker":

                def worker_diagnostic(code: str, message: str) -> None:
                    diagnostics.append(
                        PluginDiagnostic(
                            code=code,
                            phase="worker",
                            severity="warning",
                            message=redactor.redact(message),
                            plugin_id=candidate.plugin_id,
                            version=candidate.version,
                            plugin_class=candidate.plugin_class,
                            release_id=candidate.release_id,
                            package_digest=candidate.package_digest,
                            builtin_contract_hash=candidate.builtin_contract_hash,
                            generation_id=generation.generation_id,
                        )
                    )
                    if code == "worker_exit":
                        # A failed worker cannot finish its old execution contract.
                        # Revoke this exact owner, including a retiring generation
                        # still referenced by a model response or approval view.
                        active.revoke(cancel=True)
                        revoke_dependent_generations(
                            (*self._active.values(), *self._retired.values()),
                            {active.record.generation_id},
                        )
                        if self._active.get(candidate.plugin_id) is active:
                            self._schedule_generation_failure(active)

                for slot in configuration.credential_refs:
                    if slot not in resolved_credentials:
                        resolved_credentials[slot] = credential_slots.resolve(slot)
                active.instance = WorkerPluginInstance(
                    candidate.manifest,
                    package_root=package_root,
                    package_digest=candidate.release_id,
                    permissions=frozenset(request.id for request in candidate.manifest.permissions),
                    configuration=configuration.values_dict(),
                    data_directory=self.store.data_root / candidate.plugin_id,
                    credentials=resolved_credentials,
                    diagnostic=worker_diagnostic,
                    environment=environment,
                    services=active.services,
                    processes=processes,
                    models=native_run_ports,
                    inputs=native_run_ports,
                    tools=native_run_ports,
                    tool_groups=active.tool_groups,
                    scheduler=scheduler,
                )
            _validate_instance(active.instance)
            contributions = await active.instance.prepare()
            initial_tool_groups: list[ToolGroupPublication] = []
            if isinstance(contributions, sdk.ContributionSet):
                initial_tool_groups = [
                    ToolGroupPublication(group) for group in contributions.tool_groups
                ]
                if isinstance(active.instance, WorkerPluginInstance):
                    initial_tool_groups.extend(active.instance.initial_tool_groups)
                    active.instance.initial_tool_groups.clear()
                active.services.prepare(candidate.manifest.exports, contributions.services)
                contributions = adapt_sdk_contributions(contributions, owner_id=candidate.plugin_id)
            else:
                active.services.prepare(candidate.manifest.exports, ())
            if not isinstance(contributions, PluginContributions):
                raise TypeError("plugin prepare() must return SDK ContributionSet")
            contributions.providers = [
                replace(item, redactor=redactor) for item in contributions.providers
            ]
            prepared_resources = await prepare_generation_resources(
                GenerationResourceRequest(
                    candidate=candidate,
                    generation=generation,
                    package_root=package_root,
                    configuration=configuration,
                    credential_resolver=self.credential_resolver,
                    own_resource=resources.append,
                )
            )
            prepared = self.coordinator.prepare(
                candidate,
                generation,
                contributions,
                resource_skills=prepared_resources.skills,
                discovered_tools=prepared_resources.discovered_tools,
                resource_contracts=prepared_resources.resource_contracts,
            )
            if prepared_resources.degraded_reasons:
                prepared = replace(
                    prepared,
                    degraded_reasons=(
                        *prepared.degraded_reasons,
                        *prepared_resources.degraded_reasons,
                    ),
                )
            generation.contract_hashes = dict(prepared.contract_hashes)
            generation.execution_contract_hash = build_execution_contract_hash(
                plugin_id=generation.plugin_id,
                version=generation.version,
                plugin_class=generation.plugin_class,
                release_id=candidate.release_id,
                runtime_kind=generation.runtime_kind,
                configuration_revision_id=generation.configuration_revision_id,
                dependency_lock_fingerprint=generation.dependency_lock_fingerprint,
                plugin_api_version=generation.plugin_api_version,
                worker_protocol_version=generation.worker_protocol_version,
                contribution_contract_hashes=generation.contract_hashes,
            )
            prepared = bind_execution_contract(
                prepared,
                generation.execution_contract_hash,
            )
            active.contributions = prepared
            if active.tool_groups is not None:
                active.tool_groups.prepare(initial_tool_groups)
            elif initial_tool_groups:
                raise ValueError("plugin returned undeclared tool groups")
            await active.instance.start()
            if isinstance(active.instance, WorkerPluginInstance) and not active.instance.is_healthy:
                raise RuntimeError("plugin worker exited during candidate activation")
            if generation.status == "failed":
                raise RuntimeError("plugin generation failed during candidate activation")
            generation.status = "degraded" if prepared.degraded_reasons else "active"
            for reason in prepared.degraded_reasons:
                diagnostics.append(
                    PluginDiagnostic(
                        code="plugin_optional_contribution_unavailable",
                        phase="prepare",
                        severity="warning",
                        message=f"optional contribution is unavailable: {redact_text(reason)}",
                        plugin_id=candidate.plugin_id,
                        version=candidate.version,
                        plugin_class=candidate.plugin_class,
                        release_id=candidate.release_id,
                        package_digest=candidate.package_digest,
                        builtin_contract_hash=candidate.builtin_contract_hash,
                        generation_id=generation.generation_id,
                    )
                )
            return active
        except BaseException as exc:
            await self._discard_candidates((active,))
            if isinstance(exc, Exception):
                raise PluginGenerationStartError(
                    f"{type(exc).__name__}: {redactor.redact(str(exc))}"
                ) from None
            raise

    async def _stop_generation(
        self,
        generation: PluginGeneration,
        *,
        reason: StopReason,
    ) -> None:
        await generation.stop(reason=reason, timeout=self.stop_timeout_seconds)
        self._diagnostics.extend(generation.diagnostics)
        generation.diagnostics.clear()
        self._retired.pop(generation.record.generation_id, None)

    def _plugin_available(self, state: PluginStateDocument, plugin_id: str) -> bool:
        return plugin_id in state.plugins or self.builtin_plugins.get(plugin_id) is not None

    def _desired_enabled(self, state: PluginStateDocument, plugin_id: str) -> bool:
        definition = self.builtin_plugins.get(plugin_id)
        if definition is not None:
            return builtin_desired_enabled(state, definition.dependency_contract())
        return _state_plugin_record(state, plugin_id).activation.desired_enabled

    def _set_desired_enabled(
        self,
        state: PluginStateDocument,
        plugin_id: str,
        desired_enabled: bool,
    ) -> None:
        if self.builtin_plugins.get(plugin_id) is not None:
            state.builtin_activation_overrides[plugin_id] = BuiltinPluginActivationOverride(
                desired_enabled=desired_enabled
            )
            return
        _state_plugin_record(state, plugin_id).activation.desired_enabled = desired_enabled

    def _runtime_candidate(
        self,
        state: PluginStateDocument,
        plugin_id: str,
    ) -> PluginRuntimeCandidate:
        definition = self.builtin_plugins.get(plugin_id)
        if definition is not None:
            if plugin_id in state.plugins:
                raise PluginDependencyError(
                    f"builtin plugin identity is also installed as a user plugin: {plugin_id}"
                )
            builtin_configuration = create_configuration_revision()
            return PluginRuntimeCandidate(
                plugin_class="builtin",
                manifest=definition.manifest,
                release_id=definition.contract_hash,
                root=definition.root,
                configuration=builtin_configuration,
                source_kind="builtin",
                public_source="bundled with OURO",
                builtin_contract_hash=definition.contract_hash,
                factory=definition.factory,
            )
        record = _state_plugin_record(state, plugin_id)
        installed = record.selected()
        if installed is None:
            raise ValueError(f"plugin has no selected install: {plugin_id}")
        configuration = record.activation.configuration_for(installed.package_digest)
        if configuration is None:
            raise RuntimeError("plugin configuration revision is unavailable")
        if record.activation.active_configuration_revision_id != configuration.revision_id:
            raise RuntimeError("plugin active configuration revision is inconsistent")
        return PluginRuntimeCandidate(
            plugin_class="user",
            manifest=installed.manifest,
            release_id=installed.package_digest,
            root=self.store.package_path(plugin_id, installed.package_digest),
            configuration=configuration,
            source_kind=installed.provenance.source_kind,
            public_source=installed.provenance.public_source,
            package_digest=installed.package_digest,
        )

    def _record_failure(
        self,
        candidate: PluginRuntimeCandidate,
        phase: str,
        exc: Exception,
    ) -> None:
        diagnostic = PluginDiagnostic(
            code="plugin_activation_failed",
            phase=phase,
            message=f"{type(exc).__name__}: {redact_text(str(exc))}",
            plugin_id=candidate.plugin_id,
            version=candidate.version,
            plugin_class=candidate.plugin_class,
            release_id=candidate.release_id,
            package_digest=candidate.package_digest,
            builtin_contract_hash=candidate.builtin_contract_hash,
            retryable=True,
            suggested_action="Review plugin diagnostics and retry activation.",
        )
        self._diagnostics.append(diagnostic)
        self._failed[candidate.plugin_id] = candidate.release_id
        self.store.append_audit(
            PluginAuditEvent(
                action="activation",
                plugin_id=candidate.plugin_id,
                package_digest=candidate.package_digest,
                result="failed",
                detail=diagnostic.message,
                metadata={
                    "plugin_class": candidate.plugin_class,
                    "release_id": candidate.release_id,
                },
            )
        )


class _DeclarativePluginInstance:
    async def prepare(self) -> PluginContributions:
        return PluginContributions()

    async def start(self) -> None:
        return None

    async def stop(self, reason: StopReason, deadline: float) -> None:
        del reason, deadline


def _validate_instance(instance: object) -> None:
    for method in ("prepare", "start", "stop"):
        if not callable(getattr(instance, method, None)):
            raise TypeError(f"plugin instance does not implement {method}()")


def _state_plugin_record(state: PluginStateDocument, plugin_id: str) -> PluginRecord:
    try:
        return state.plugins[plugin_id]
    except KeyError as exc:
        raise KeyError(f"plugin is not installed: {plugin_id}") from exc


def _retirement_reason(
    reason: str,
    *,
    replaced: bool,
    old: PluginGeneration,
    new: PluginGeneration | None,
) -> StopReason:
    if not replaced:
        return "disable" if reason == "disable" else "restart"
    if new is not None and new.candidate.release_id != old.candidate.release_id:
        return "upgrade"
    return "restart"
