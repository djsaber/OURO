from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any, Literal

from ouro.core.base import new_id
from ouro.core.redaction import SensitiveValueRedactor, redact_text
from ouro.plugins.api import (
    BoundPluginSessionStateStore,
    LoadedPluginModule,
    OwnedTaskGroup,
    ScopedPluginDataStore,
    adapt_sdk_contributions,
    build_plugin_context,
    load_plugin_factory,
)
from ouro.plugins.authorization import plugin_install_readiness
from ouro.plugins.builtin import EMPTY_BUILTIN_PLUGIN_REGISTRY, BuiltinPluginRegistry
from ouro.plugins.candidates import PluginRuntimeCandidate
from ouro.plugins.catalog import (
    ExtensionCatalog,
    ExtensionCatalogBase,
    ExtensionCatalogLease,
    GenerationGate,
    build_extension_catalog,
    generation_matches_execution_contract,
)
from ouro.plugins.contributions import (
    ContributionCoordinator,
    PluginContributions,
    PreparedContributionSet,
    bind_execution_contract,
)
from ouro.plugins.dependencies import (
    PluginDependencyError,
    PluginLockGraph,
    active_dependents,
    builtin_desired_enabled,
    desired_dependant_closure,
    plugin_root_sort_key,
    resolve_plugin_dependencies,
)
from ouro.plugins.execution_contracts import (
    build_dependency_lock_fingerprint,
    build_execution_contract_hash,
)
from ouro.plugins.generation_resources import (
    GenerationResourceRequest,
    prepare_generation_resources,
)
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
from ouro.plugins.package import PLUGIN_API_VERSION
from ouro.plugins.store import PluginPackageStore, PluginStateConflictError
from ouro.plugins.worker import WorkerPluginInstance
from ouro.sdk.plugins import v1 as sdk
from ouro.sessions.base import SessionRepository
from ouro.skills import SkillMetadata

StopReason = Literal["disable", "restart", "upgrade", "shutdown", "startup_failed"]


class PluginGenerationStartError(RuntimeError):
    pass


class PluginCandidateBuildError(RuntimeError):
    def __init__(self, plugin_id: str, cause: Exception) -> None:
        super().__init__(f"candidate generation failed for {plugin_id}: {cause}")
        self.plugin_id = plugin_id
        self.cause = cause


class PluginExecutionContractUnavailableError(RuntimeError):
    """Raised when persisted plugin code cannot be reconstructed exactly."""


@dataclass
class ActivePluginGeneration:
    record: PluginGenerationRecord
    candidate: PluginRuntimeCandidate
    instance: Any
    contributions: PreparedContributionSet
    gate: GenerationGate
    tasks: OwnedTaskGroup
    data_store: ScopedPluginDataStore | None = None
    session_state_store: BoundPluginSessionStateStore | None = None
    module: LoadedPluginModule | None = None
    resources: list[Any] = field(default_factory=list)
    diagnostics: list[PluginDiagnostic] = field(default_factory=list)
    lease_count: int = 0
    retiring: bool = False
    stopped: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    service_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    started_services: list[Any] = field(default_factory=list, repr=False)

    def acquire(self) -> PluginGenerationLease:
        if self.stopped:
            raise RuntimeError(f"plugin generation is stopped: {self.record.generation_id}")
        self.lease_count += 1
        self.drained.clear()
        return PluginGenerationLease(self)

    def release(self) -> None:
        if self.lease_count:
            self.lease_count -= 1
        if self.lease_count == 0:
            self.drained.set()


class PluginGenerationLease:
    def __init__(self, generation: ActivePluginGeneration) -> None:
        self._generation = generation
        self._closed = False

    @property
    def record(self) -> PluginGenerationRecord:
        return self._generation.record

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation.release()

    def __enter__(self) -> PluginGenerationLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


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
        self._active: dict[str, ActivePluginGeneration] = {}
        self._retired: dict[str, ActivePluginGeneration] = {}
        self._failed: dict[str, str] = {}
        self._diagnostics: list[PluginDiagnostic] = []
        self._lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._catalog_pointer_lock = threading.Lock()
        self._catalog_base: ExtensionCatalogBase | None = None
        self._current_catalog: ExtensionCatalog | None = None
        self._catalogs: dict[str, ExtensionCatalog] = {}
        self._retirement_tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        if self._started:
            return
        self._ensure_catalog()
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
        desired_roots = sorted(
            {
                *(
                    plugin_id
                    for plugin_id, record in state.plugins.items()
                    if record.activation.desired_enabled
                ),
                *(
                    plugin_id
                    for plugin_id, contract in self.builtin_contracts.items()
                    if builtin_desired_enabled(state, contract)
                ),
            },
            key=lambda plugin_id: plugin_root_sort_key(plugin_id, state),
        )
        for plugin_id in desired_roots:
            try:
                await self.activate(plugin_id)
            except Exception as exc:  # noqa: BLE001 - isolate independent roots.
                self._diagnostics.append(
                    PluginDiagnostic(
                        code="plugin_startup_failed",
                        phase="startup",
                        message=f"{type(exc).__name__}: {redact_text(str(exc))}",
                        plugin_id=plugin_id,
                        suggested_action="Review plugin diagnostics and retry activation.",
                    )
                )

    async def activate(self, plugin_id: str) -> PluginStatus:
        async with self._mutation_lock:
            self._ensure_catalog()
            expected = self.store.read_state()
            candidate = self._runtime_candidate(expected, plugin_id)
            status = self.status(plugin_id)
            if plugin_id in self._active:
                status = "ready"
            if status == "failed" and self._failed.get(plugin_id) == candidate.release_id:
                self._failed.pop(plugin_id, None)
                status = self.status(plugin_id)
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
            return "active"

    async def disable(self, plugin_id: str, *, wait: bool = True) -> PluginStatus:
        if not wait:
            raise ValueError("non-blocking plugin disable is not supported")
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
            return "disabled"

    async def suspend(self, plugin_id: str, *, revoke: bool = False) -> PluginStatus:
        """Withdraw an active generation without changing desired state."""

        async with self._mutation_lock:
            expected = self.store.read_state()
            self._runtime_candidate(expected, plugin_id)
            affected = (
                desired_dependant_closure(
                    expected,
                    {plugin_id},
                    builtin_contracts=self.builtin_contracts,
                )
                if revoke
                else {plugin_id}
            )
            await self._transition_state_locked(
                expected=expected,
                candidate_state=expected,
                remove_ids=affected,
                close_gate_ids=affected if revoke else set(),
                reason="revoke" if revoke else "restart",
            )
            return self.status(plugin_id)

    async def enable(self, plugin_id: str) -> PluginStatus:
        async with self._mutation_lock:
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

    def snapshot(self) -> ExtensionCatalogLease:
        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        self._ensure_catalog()
        with self._catalog_pointer_lock:
            catalog = self._current_catalog
            assert catalog is not None
            return catalog.acquire(on_close=self._catalog_lease_closed)

    async def snapshot_for(
        self,
        expected_generations: list[dict[str, object]],
        *,
        catalog_id: str | None = None,
    ) -> ExtensionCatalogLease:
        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        self._ensure_catalog()
        catalog = self._matching_catalog(expected_generations, catalog_id=catalog_id)
        if catalog is not None:
            return catalog.acquire(on_close=self._catalog_lease_closed)
        async with self._mutation_lock:
            catalog = self._matching_catalog(expected_generations, catalog_id=catalog_id)
            if catalog is None:
                catalog = await self._rebuild_recovery_catalog(expected_generations)
            return catalog.acquire(on_close=self._catalog_lease_closed)

    def _matching_catalog(
        self,
        expected_generations: list[dict[str, object]],
        *,
        catalog_id: str | None,
    ) -> ExtensionCatalog | None:
        with self._catalog_pointer_lock:
            catalog = self._catalogs.get(catalog_id) if catalog_id is not None else None
            if catalog is None:
                catalog = next(
                    (
                        item
                        for item in self._catalogs.values()
                        if item.matches(expected_generations)
                    ),
                    None,
                )
            if catalog is None or not catalog.matches(expected_generations):
                return None
            return catalog

    async def _rebuild_recovery_catalog(
        self,
        expected_generations: list[dict[str, object]],
    ) -> ExtensionCatalog:
        """Rebuild an exact durable contract without changing desired plugin state."""

        expected_records = {
            record.plugin_id: record
            for record in (
                PluginGenerationRecord.model_validate(item) for item in expected_generations
            )
        }
        if len(expected_records) != len(expected_generations):
            raise PluginExecutionContractUnavailableError(
                "required plugin catalog has duplicate plugin identities"
            )
        if not expected_records:
            catalog = self._build_catalog({})
            with self._catalog_pointer_lock:
                self._catalogs[catalog.catalog_id] = catalog
            return catalog

        state = self.store.read_state()
        recovery_state = state.model_copy(deep=True)
        for plugin_id, expected in expected_records.items():
            if not self._desired_enabled(recovery_state, plugin_id):
                raise PluginExecutionContractUnavailableError(
                    "required plugin catalog is no longer available"
                )
            if expected.plugin_class == "builtin":
                definition = self.builtin_plugins.get(plugin_id)
                if (
                    definition is None
                    or definition.contract_hash != expected.builtin_contract_hash
                    or create_configuration_revision().revision_id
                    != expected.configuration_revision_id
                ):
                    raise PluginExecutionContractUnavailableError(
                        "required builtin plugin contract is no longer available"
                    )
                continue
            record = recovery_state.plugins.get(plugin_id)
            if record is None:
                raise PluginExecutionContractUnavailableError(
                    "required plugin catalog is no longer available"
                )
            installed = next(
                (
                    item
                    for item in record.installs
                    if item.package_digest == expected.package_digest
                ),
                None,
            )
            revision = record.activation.configuration_revisions.get(
                expected.configuration_revision_id
            )
            if installed is None or revision is None:
                raise PluginExecutionContractUnavailableError(
                    "required plugin catalog is no longer available"
                )
            record.activation.selected_digest = installed.package_digest
            record.activation.configuration_revision_by_digest[installed.package_digest] = (
                revision.revision_id
            )
            record.activation.active_configuration_revision_id = revision.revision_id

        recovery_state = PluginStateDocument.model_validate(
            recovery_state.model_dump(mode="python")
        )
        try:
            graph = self._resolve_dependencies(
                recovery_state,
                roots=set(expected_records),
            )
        except PluginDependencyError as exc:
            raise PluginExecutionContractUnavailableError(
                "required plugin catalog is no longer available"
            ) from exc
        if {node.plugin_id for node in graph.nodes} != set(expected_records):
            raise PluginExecutionContractUnavailableError(
                "required plugin catalog dependency lock is incomplete"
            )

        sources: dict[str, ActivePluginGeneration] = {}
        rebuilt: list[ActivePluginGeneration] = []
        try:
            for node in graph.nodes:
                expected = expected_records[node.plugin_id]
                active = self._active.get(node.plugin_id)
                if active is not None and generation_matches_execution_contract(
                    active.record,
                    expected.model_dump(mode="python"),
                ):
                    sources[node.plugin_id] = active
                    continue
                if self._candidate_readiness(recovery_state, node.plugin_id) != "ready":
                    raise PluginExecutionContractUnavailableError(
                        "required plugin catalog is no longer available"
                    )
                dependency_graph = self._resolve_dependencies(
                    recovery_state,
                    roots={node.plugin_id},
                )
                dependency_fingerprint = build_dependency_lock_fingerprint(
                    dependency_graph,
                    recovery_state,
                    {plugin_id: generation.record for plugin_id, generation in sources.items()},
                    owner_plugin_id=node.plugin_id,
                )
                generation = await self._build_generation(
                    self._runtime_candidate(recovery_state, node.plugin_id),
                    dependency_lock_fingerprint=dependency_fingerprint,
                )
                if not generation_matches_execution_contract(
                    generation.record,
                    expected.model_dump(mode="python"),
                ):
                    await self._stop_generation(generation, reason="startup_failed")
                    raise PluginExecutionContractUnavailableError(
                        "required plugin execution contract does not match"
                    )
                sources[node.plugin_id] = generation
                rebuilt.append(generation)

            catalog = self._build_catalog(sources, state=recovery_state)
            if not catalog.matches(expected_generations):
                raise PluginExecutionContractUnavailableError(
                    "required plugin execution contract does not match"
                )
            for generation in rebuilt:
                if generation.data_store is not None:
                    generation.data_store.activate()
                if generation.session_state_store is not None:
                    generation.session_state_store.activate()
                generation.tasks.activate()
                generation.gate.open()
            lease = catalog.acquire()
            with self._catalog_pointer_lock:
                self._catalogs[catalog.catalog_id] = catalog
            for generation in rebuilt:
                self._schedule_retirement(generation, reason="restart")
            lease.close()
            return catalog
        except BaseException as exc:
            for generation in reversed(rebuilt):
                if not generation.retiring:
                    with suppress(Exception):
                        await self._stop_generation(generation, reason="startup_failed")
            if isinstance(exc, PluginExecutionContractUnavailableError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise PluginExecutionContractUnavailableError(
                "required plugin catalog is no longer available"
            ) from exc

    def project_status(self, record: PluginRecord) -> PluginStatus:
        installed = record.selected()
        if installed is None:
            return "invalid"
        active = self._active.get(record.plugin_id)
        if active is not None:
            if isinstance(active.instance, WorkerPluginInstance) and not active.instance.is_healthy:
                active.gate.close()
                self._schedule_generation_failure(active)
            return active.record.status
        if self._failed.get(record.plugin_id) == installed.package_digest:
            return "failed"
        if not record.activation.desired_enabled:
            return "disabled"
        package_root = self.store.package_path(record.plugin_id, installed.package_digest)
        readiness = plugin_install_readiness(installed, record.activation, package_root)
        if readiness != "ready":
            return readiness
        state = self.store.read_state()
        if self._dependency_resolution_error(state, record.plugin_id) is not None:
            return "incompatible"
        if self._dependency_blockers(state, record.plugin_id):
            return "blocked"
        return "ready"

    def project_builtin_status(self, plugin_id: str) -> PluginStatus:
        state = self.store.read_state()
        definition = self.builtin_plugins.require(plugin_id)
        active = self._active.get(plugin_id)
        if active is not None:
            return active.record.status
        if self._failed.get(plugin_id) == definition.contract_hash:
            return "failed"
        if not self._desired_enabled(state, plugin_id):
            return "disabled"
        error = self._dependency_resolution_error(state, plugin_id)
        if error is not None:
            return "incompatible"
        if self._dependency_blockers(state, plugin_id):
            return "blocked"
        return "ready"

    def status(self, plugin_id: str) -> PluginStatus:
        if self.builtin_plugins.get(plugin_id) is not None:
            return self.project_builtin_status(plugin_id)
        return self.project_status(self._plugin_record(plugin_id))

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
        if self._closed:
            return
        self._closed = True
        self._ensure_catalog()
        async with self._lock:
            state = self.store.read_state()
            try:
                active_order = self._resolve_dependencies(
                    state,
                    roots=set(self._active),
                ).activation_order
            except PluginDependencyError:
                active_order = tuple(sorted(self._active))
            generations = [
                *(
                    self._active[plugin_id]
                    for plugin_id in active_order
                    if plugin_id in self._active
                ),
                *self._retired.values(),
            ]
            for generation in generations:
                generation.retiring = True
                generation.gate.close()
                if generation.lease_count == 0:
                    generation.drained.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(generation.drained.wait() for generation in generations),
                ),
                timeout=self.stop_timeout_seconds,
            )
        except TimeoutError:
            self._diagnostics.append(
                PluginDiagnostic(
                    code="plugin_shutdown_forced",
                    phase="shutdown",
                    severity="warning",
                    message="plugin generation leases exceeded the Host shutdown deadline",
                    suggested_action="Inspect runs or plugin callbacks that did not finish.",
                )
            )
        async with self._lock:
            self._active.clear()
            self._retired.clear()
        retirement_tasks = tuple(self._retirement_tasks)
        if retirement_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*retirement_tasks, return_exceptions=True),
                    timeout=self.stop_timeout_seconds,
                )
            except TimeoutError:
                for task in retirement_tasks:
                    task.cancel()
                await asyncio.gather(*retirement_tasks, return_exceptions=True)
        self._retirement_tasks.clear()
        errors: list[Exception] = []
        seen: set[str] = set()
        for generation in reversed(generations):
            if generation.record.generation_id in seen:
                continue
            seen.add(generation.record.generation_id)
            try:
                await self._stop_generation(generation, reason="shutdown")
            except Exception as exc:  # noqa: BLE001 - close every plugin owner.
                errors.append(exc)
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
        reason: str,
    ) -> None:
        """Prepare a complete candidate before committing state and catalog together."""

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
        known_ids = {*expected.plugins, *self.builtin_plugins.plugin_ids}
        unknown = (requested_rebuilds | requested_removals | gated_removals) - known_ids
        if unknown:
            raise KeyError(f"plugin is not available: {sorted(unknown)[0]}")
        requested_rebuilds -= requested_removals

        graph = (
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

        candidates: dict[str, ActivePluginGeneration] = {}
        published = False
        try:
            if graph is not None:
                for node in graph.nodes:
                    if node.plugin_id not in build_ids:
                        continue
                    runtime_candidate = self._runtime_candidate(candidate_state, node.plugin_id)
                    readiness = self._candidate_readiness(candidate_state, node.plugin_id)
                    if readiness != "ready":
                        raise PluginGenerationStartError(
                            f"plugin candidate is not activation-ready: "
                            f"{node.plugin_id}: {readiness}"
                        )
                    for dependency_id in node.dependencies:
                        dependency_node = next(
                            item for item in graph.nodes if item.plugin_id == dependency_id
                        )
                        dependency = candidates.get(dependency_id) or self._active.get(
                            dependency_id
                        )
                        if dependency is None or dependency_id in requested_removals:
                            raise PluginDependencyError(
                                f"required plugin generation is unavailable: {dependency_id}"
                            )
                        expected_release_id = (
                            dependency_node.package_digest or dependency_node.contract_hash
                        )
                        if dependency.candidate.release_id != expected_release_id:
                            raise PluginDependencyError(
                                f"required plugin generation is stale: {dependency_id}"
                            )
                    try:
                        dependency_graph = self._resolve_dependencies(
                            candidate_state,
                            roots={node.plugin_id},
                        )
                        dependency_generations = {
                            plugin_id: generation.record
                            for plugin_id, generation in self._active.items()
                            if plugin_id not in requested_removals
                        }
                        dependency_generations.update(
                            {
                                plugin_id: generation.record
                                for plugin_id, generation in candidates.items()
                            }
                        )
                        dependency_fingerprint = build_dependency_lock_fingerprint(
                            dependency_graph,
                            candidate_state,
                            dependency_generations,
                            owner_plugin_id=node.plugin_id,
                        )
                        candidates[node.plugin_id] = await self._build_generation(
                            runtime_candidate,
                            dependency_lock_fingerprint=dependency_fingerprint,
                        )
                    except Exception as exc:
                        raise PluginCandidateBuildError(node.plugin_id, exc) from exc

            next_generations = {
                plugin_id: generation
                for plugin_id, generation in self._active.items()
                if plugin_id not in requested_removals and plugin_id not in build_ids
            }
            next_generations.update(candidates)
            catalog = self._build_catalog(next_generations, state=candidate_state)
            self.store.commit_candidate_state(expected, candidate_state)

            previous = {
                plugin_id: generation
                for plugin_id, generation in self._active.items()
                if plugin_id in requested_removals or plugin_id in build_ids
            }
            for plugin_id in gated_removals:
                generation = previous.get(plugin_id)
                if generation is not None:
                    generation.gate.close()
            for generation in candidates.values():
                if generation.data_store is not None:
                    try:
                        generation.data_store.activate()
                    except Exception as exc:  # noqa: BLE001 - publication remains authoritative.
                        generation.diagnostics.append(
                            PluginDiagnostic(
                                code="plugin_candidate_data_commit_failed",
                                phase="activate",
                                message=f"{type(exc).__name__}: {redact_text(str(exc))}",
                                plugin_id=generation.candidate.plugin_id,
                                version=generation.candidate.version,
                                plugin_class=generation.candidate.plugin_class,
                                release_id=generation.candidate.release_id,
                                package_digest=generation.candidate.package_digest,
                                builtin_contract_hash=(generation.candidate.builtin_contract_hash),
                                generation_id=generation.record.generation_id,
                                suggested_action="Inspect the plugin data directory and retry.",
                            )
                        )
                if generation.session_state_store is not None:
                    generation.session_state_store.activate()
                generation.tasks.activate()
                generation.gate.open()
            self._active = next_generations
            for plugin_id in candidates.keys() | requested_removals:
                self._failed.pop(plugin_id, None)
            self._publish_catalog(catalog)
            published = True

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
            await self._handover_background_services(
                previous,
                candidates,
                graph=graph,
            )
        except BaseException:
            if not published:
                for generation in reversed(tuple(candidates.values())):
                    with suppress(Exception):
                        await self._stop_generation(generation, reason="startup_failed")
            raise

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
            if self._current_catalog is not None:
                return self._current_catalog
            self._catalog_base = ExtensionCatalogBase.capture(
                tools=self.coordinator.tools,
                observers=self.coordinator.observers,
                context_providers=self.coordinator.context_providers,
                commands=self.coordinator.commands,
                compaction=self.coordinator.compaction,
                continuations=self.coordinator.continuations,
                prompt_policies=self.coordinator.prompt_policies,
                turn_policies=self.coordinator.turn_policies,
                tool_policies=self.coordinator.tool_policies,
                skills=self.base_skills,
            )
            catalog = build_extension_catalog(self._catalog_base, ())
            self._current_catalog = catalog
            self._catalogs[catalog.catalog_id] = catalog
            return catalog

    def _build_catalog(
        self,
        generations: dict[str, ActivePluginGeneration],
        *,
        state: PluginStateDocument | None = None,
    ) -> ExtensionCatalog:
        self._ensure_catalog()
        assert self._catalog_base is not None
        if not generations:
            ordered_ids: tuple[str, ...] = ()
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
        )

    def _publish_catalog(self, catalog: ExtensionCatalog) -> None:
        with self._catalog_pointer_lock:
            previous = self._current_catalog
            self._catalogs[catalog.catalog_id] = catalog
            self._current_catalog = catalog
        if previous is not None:
            self._discard_catalog_if_idle(previous)

    def _catalog_lease_closed(self, catalog: ExtensionCatalog) -> None:
        self._discard_catalog_if_idle(catalog)

    def _discard_catalog_if_idle(self, catalog: ExtensionCatalog) -> None:
        with self._catalog_pointer_lock:
            if catalog is self._current_catalog or catalog.lease_count != 0:
                return
            self._catalogs.pop(catalog.catalog_id, None)

    def _schedule_retirement(
        self,
        generation: ActivePluginGeneration,
        *,
        reason: StopReason,
    ) -> None:
        generation.retiring = True
        self._retired[generation.record.generation_id] = generation
        if generation.lease_count == 0:
            generation.drained.set()
        task = asyncio.create_task(
            self._retire_generation(generation, reason=reason),
            name=f"ouro-plugin-retire-{generation.record.generation_id}",
        )
        self._retirement_tasks.add(task)
        task.add_done_callback(self._retirement_tasks.discard)

    def _schedule_generation_failure(self, generation: ActivePluginGeneration) -> None:
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
        generation: ActivePluginGeneration,
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
        generation: ActivePluginGeneration,
        *,
        reason: StopReason,
    ) -> None:
        await generation.drained.wait()
        try:
            await self._stop_generation(generation, reason=reason)
        except Exception as exc:  # noqa: BLE001 - retirement cannot corrupt current catalog.
            self._diagnostics.append(
                PluginDiagnostic(
                    code="plugin_retirement_failed",
                    phase="retire",
                    message=f"{type(exc).__name__}: {redact_text(str(exc))}",
                    plugin_id=generation.candidate.plugin_id,
                    version=generation.candidate.version,
                    plugin_class=generation.candidate.plugin_class,
                    release_id=generation.candidate.release_id,
                    package_digest=generation.candidate.package_digest,
                    builtin_contract_hash=generation.candidate.builtin_contract_hash,
                    generation_id=generation.record.generation_id,
                    suggested_action="Restart OURO to retry complete plugin cleanup.",
                )
            )

    async def _handover_background_services(
        self,
        previous: dict[str, ActivePluginGeneration],
        candidates: dict[str, ActivePluginGeneration],
        *,
        graph,
    ) -> None:
        shutdown_order = (
            graph.shutdown_order if graph is not None else tuple(sorted(previous, reverse=True))
        )
        for plugin_id in shutdown_order:
            generation = previous.get(plugin_id)
            if generation is not None:
                try:
                    await self._stop_generation_services(generation)
                except Exception as exc:  # noqa: BLE001 - catalog publication is authoritative.
                    generation.diagnostics.append(
                        PluginDiagnostic(
                            code="plugin_background_service_stop_failed",
                            phase="background",
                            message=f"{type(exc).__name__}: {redact_text(str(exc))}",
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
        generation: ActivePluginGeneration,
    ) -> None:
        async with generation.service_lock:
            if generation.started_services or generation.stopped:
                return
            try:
                for service in generation.contributions.background_services:
                    await service.start()
                    generation.started_services.append(service)
            except Exception as exc:  # noqa: BLE001 - contribution catalog stays usable.
                generation.record.status = "degraded"
                generation.diagnostics.append(
                    PluginDiagnostic(
                        code="plugin_background_service_start_failed",
                        phase="background",
                        message=f"{type(exc).__name__}: {redact_text(str(exc))}",
                        plugin_id=generation.candidate.plugin_id,
                        version=generation.candidate.version,
                        plugin_class=generation.candidate.plugin_class,
                        release_id=generation.candidate.release_id,
                        package_digest=generation.candidate.package_digest,
                        builtin_contract_hash=generation.candidate.builtin_contract_hash,
                        generation_id=generation.record.generation_id,
                        suggested_action="Fix the background service and restart the plugin.",
                    )
                )
                for service in reversed(generation.started_services):
                    with suppress(Exception):
                        await service.stop()
                generation.started_services.clear()

    async def _stop_generation_services(
        self,
        generation: ActivePluginGeneration,
    ) -> None:
        async with generation.service_lock:
            errors: list[Exception] = []
            for service in reversed(generation.started_services):
                try:
                    await service.stop()
                except Exception as exc:  # noqa: BLE001 - stop every owned service.
                    errors.append(exc)
            generation.started_services.clear()
            if errors:
                raise ExceptionGroup(
                    f"plugin background service shutdown failed: {generation.record.generation_id}",
                    errors,
                )

    async def _build_generation(
        self,
        candidate: PluginRuntimeCandidate,
        *,
        dependency_lock_fingerprint: str,
    ) -> ActivePluginGeneration:
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
        module: LoadedPluginModule | None = None
        resources: list[Any] = []
        instance: Any = _DeclarativePluginInstance()
        prepared: PreparedContributionSet | None = None
        data_store: ScopedPluginDataStore | None = None
        session_state_store: BoundPluginSessionStateStore | None = None
        package_root = (
            self.store.verify_package(candidate.plugin_id, candidate.package_digest)
            if candidate.package_digest is not None
            else candidate.root
        )
        try:
            if candidate.manifest.runtime.kind == "python-inprocess":
                if candidate.plugin_class == "builtin":
                    factory = candidate.factory
                    assert factory is not None
                else:
                    entrypoint = candidate.manifest.runtime.entrypoint
                    assert entrypoint is not None
                    factory, module = load_plugin_factory(
                        package_root,
                        entrypoint,
                        module_namespace=plugin_module_namespace(candidate.manifest.namespace),
                        generation_id=generation.generation_id,
                    )
                data_store = ScopedPluginDataStore(
                    self.store.data_root / candidate.plugin_id,
                    staged=True,
                )
                if self.session_repository is not None:
                    session_state_store = BoundPluginSessionStateStore(
                        self.session_repository,
                        candidate.plugin_id,
                    )
                context = build_plugin_context(
                    candidate,
                    generation,
                    data_root=self.store.data_root,
                    configuration=configuration.values,
                    credential_bindings=configuration.credential_refs,
                    credential_resolver=self.credential_resolver,
                    tasks=tasks,
                    diagnostics=diagnostics,
                    redactor=redactor,
                    data_store=data_store,
                    session_state_store=session_state_store,
                    artifact_root=self.store.home / "artifacts",
                    host_home=self.store.home,
                )
                instance = factory(context)
                if inspect.isawaitable(instance):
                    instance = await instance
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
                        generation_gate.close()
                        active = self._active.get(candidate.plugin_id)
                        if (
                            active is not None
                            and active.record.generation_id == generation.generation_id
                        ):
                            self._schedule_generation_failure(active)

                credentials = _resolve_worker_credentials(
                    configuration.credential_refs,
                    self.credential_resolver,
                )
                for value in credentials.values():
                    redactor.add(value)
                instance = WorkerPluginInstance(
                    candidate.manifest,
                    package_root=package_root,
                    package_digest=candidate.release_id,
                    permissions=frozenset(request.id for request in candidate.manifest.permissions),
                    configuration=configuration.values,
                    data_directory=self.store.data_root / candidate.plugin_id,
                    credentials=credentials,
                    diagnostic=worker_diagnostic,
                )
            _validate_instance(instance)
            contributions = await instance.prepare()
            if isinstance(contributions, sdk.ContributionSet):
                contributions = adapt_sdk_contributions(contributions)
            if not isinstance(contributions, PluginContributions):
                raise TypeError("plugin prepare() must return SDK ContributionSet")
            prepared_resources = await prepare_generation_resources(
                GenerationResourceRequest(
                    candidate=candidate,
                    generation=generation,
                    package_root=package_root,
                    configuration=configuration,
                    credential_resolver=self.credential_resolver,
                )
            )
            resources.extend(prepared_resources.owned_resources)
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
            await instance.start()
            if isinstance(instance, WorkerPluginInstance) and not instance.is_healthy:
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
            active = ActivePluginGeneration(
                record=generation,
                candidate=candidate,
                instance=instance,
                contributions=prepared,
                gate=generation_gate,
                tasks=tasks,
                data_store=data_store,
                session_state_store=session_state_store,
                module=module,
                resources=resources,
                diagnostics=diagnostics,
            )
            return active
        except BaseException as exc:
            with suppress(Exception):
                await _stop_instance(
                    instance,
                    reason="startup_failed",
                    timeout=self.stop_timeout_seconds,
                )
            await tasks.close(timeout=self.stop_timeout_seconds)
            if data_store is not None:
                data_store.discard()
            for resource in reversed(resources):
                with suppress(Exception):
                    await resource.close()
            if module is not None:
                module.close()
            if isinstance(exc, Exception):
                raise PluginGenerationStartError(
                    f"{type(exc).__name__}: {redactor.redact(str(exc))}"
                ) from None
            raise

    async def _stop_generation(
        self,
        generation: ActivePluginGeneration,
        *,
        reason: StopReason,
    ) -> None:
        async with generation.stop_lock:
            if generation.stopped:
                return
            generation.gate.close()
            await generation.gate.wait_drained()
            errors: list[Exception] = []
            try:
                await self._stop_generation_services(generation)
            except Exception as exc:  # noqa: BLE001 - continue owner cleanup.
                errors.append(exc)
            try:
                await _stop_instance(
                    generation.instance,
                    reason=reason,
                    timeout=self.stop_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - continue owner cleanup.
                errors.append(exc)
            await generation.tasks.close(timeout=self.stop_timeout_seconds)
            for resource in reversed(generation.resources):
                try:
                    await resource.close()
                except Exception as exc:  # noqa: BLE001 - continue owner cleanup.
                    errors.append(exc)
            if generation.module is not None:
                generation.module.close()
            if generation.session_state_store is not None:
                generation.session_state_store.deactivate()
            generation.record.status = "disabled"
            generation.stopped = True
            self._diagnostics.extend(generation.diagnostics)
            generation.diagnostics.clear()
            self._retired.pop(generation.record.generation_id, None)
            if errors:
                raise ExceptionGroup(
                    f"plugin generation shutdown failed: {generation.record.generation_id}",
                    errors,
                )

    def _plugin_record(self, plugin_id: str) -> PluginRecord:
        state = self.store.read_state()
        try:
            return state.plugins[plugin_id]
        except KeyError as exc:
            raise KeyError(f"plugin is not installed: {plugin_id}") from exc

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
    old: ActivePluginGeneration,
    new: ActivePluginGeneration | None,
) -> StopReason:
    if not replaced:
        return "disable" if reason in {"disable", "revoke"} else "restart"
    if new is not None and new.candidate.release_id != old.candidate.release_id:
        return "upgrade"
    return "restart"


async def _stop_instance(instance: Any, *, reason: StopReason, timeout: float) -> None:
    deadline = monotonic() + timeout
    await asyncio.wait_for(instance.stop(reason, deadline), timeout=timeout)


def _resolve_worker_credentials(
    bindings: dict[str, str],
    resolver: Callable[[str], str] | None,
) -> dict[str, str]:
    if not bindings:
        return {}
    if resolver is None:
        raise RuntimeError("plugin credential resolver is unavailable")
    return {slot: resolver(reference) for slot, reference in bindings.items()}
