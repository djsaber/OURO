from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from ouro.core.base import new_id
from ouro.core.hooks import HookRegistration, HookResult
from ouro.core.redaction import SensitiveValueRedactor, redact_text
from ouro.plugins.api import (
    LoadedPluginModule,
    OwnedTaskGroup,
    adapt_sdk_contributions,
    build_plugin_context,
    load_plugin_factory,
)
from ouro.plugins.authorization import (
    capability_grant_is_valid,
    code_trust_is_valid,
    configuration_errors,
)
from ouro.plugins.contributions import (
    ContributionCoordinator,
    ContributionLease,
    PluginContributions,
    PreparedContributionSet,
)
from ouro.plugins.dependencies import (
    PluginDependencyError,
    active_dependents,
    plan_desired_plugin_activation,
    resolve_plugin_dependencies,
)
from ouro.plugins.mcp import PluginMCPResource
from ouro.plugins.models import (
    InstalledPlugin,
    PluginAuditEvent,
    PluginDiagnostic,
    PluginGenerationRecord,
    PluginRecord,
    PluginStatus,
)
from ouro.plugins.package import resolve_manifest_path
from ouro.plugins.store import PluginPackageStore
from ouro.plugins.worker import WorkerPluginInstance
from ouro.prompts import render_agent_skills_prompt
from ouro.sdk.plugins import v1 as sdk
from ouro.skills import SkillLoader
from ouro.tools import Tool

StopReason = Literal["disable", "restart", "upgrade", "shutdown", "startup_failed"]


class PluginGenerationStartError(RuntimeError):
    pass


@dataclass
class ActivePluginGeneration:
    record: PluginGenerationRecord
    installed: InstalledPlugin
    instance: Any
    contributions: PreparedContributionSet
    contribution_lease: ContributionLease
    tasks: OwnedTaskGroup
    module: LoadedPluginModule | None = None
    resources: list[Any] = field(default_factory=list)
    diagnostics: list[PluginDiagnostic] = field(default_factory=list)
    lease_count: int = 0
    draining: bool = False
    stopped: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    def acquire(self) -> PluginGenerationLease:
        if self.draining or self.stopped:
            raise RuntimeError(f"plugin generation is draining: {self.record.generation_id}")
        self.lease_count += 1
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


class ExtensionCatalogSnapshot:
    """Run-scoped immutable generation identities plus leases."""

    def __init__(
        self,
        leases: tuple[PluginGenerationLease, ...],
        *,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._leases = leases
        self._on_close = on_close
        self.generations = tuple(lease.record for lease in leases)
        self._closed = False

    def contract_hash(self, canonical_identity: str) -> str | None:
        for generation in self.generations:
            if canonical_identity in generation.contract_hashes:
                return generation.contract_hashes[canonical_identity]
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for lease in reversed(self._leases):
            lease.close()
        if self._on_close is not None:
            self._on_close()

    def __enter__(self) -> ExtensionCatalogSnapshot:
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
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.credential_resolver = credential_resolver
        self.stop_timeout_seconds = stop_timeout_seconds
        self._active: dict[str, ActivePluginGeneration] = {}
        self._retired: dict[str, ActivePluginGeneration] = {}
        self._failed: dict[str, str] = {}
        self._diagnostics: list[PluginDiagnostic] = []
        self._lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._catalog_lease_count = 0
        self._catalog_drained = asyncio.Event()
        self._catalog_drained.set()
        self._catalog_mutating = False
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        if self._started:
            return
        self._started = True
        state = self.store.read_state()
        plan = plan_desired_plugin_activation(state)
        for plugin_id, exc in plan.failures:
            self._record_dependency_failure(plugin_id, exc)
        for plugin_id in plan.graph.activation_order:
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
            async with self._lock:
                record = self._plugin_record(plugin_id)
                installed = record.selected()
                if installed is None:
                    return "invalid"
                status = self.project_status(record)
                if (
                    status == "failed"
                    and self._failed.get(plugin_id) == installed.package_digest
                ):
                    self._failed.pop(plugin_id, None)
                    status = self.project_status(record)
                if status not in {"ready", "active"}:
                    return status
                previous = self._active.get(plugin_id)
                self._begin_catalog_mutation()
                if previous is not None:
                    previous.draining = True
                    self._retired[previous.record.generation_id] = previous
                    if previous.lease_count == 0:
                        previous.drained.set()
            try:
                await self._catalog_drained.wait()
                if previous is not None:
                    await previous.drained.wait()
                    await self._stop_generation(previous, reason="upgrade")
                    async with self._lock:
                        if self._active.get(plugin_id) is previous:
                            self._active.pop(plugin_id, None)

                # Grants/configuration may have changed while the old generation drained.
                record = self._plugin_record(plugin_id)
                installed = record.selected()
                if installed is None:
                    return "invalid"
                status = self.project_status(record)
                if status != "ready":
                    return status
                try:
                    candidate = await self._build_generation(installed, record)
                except Exception as exc:
                    self._record_failure(installed, "activation", exc)
                    return "failed"
                async with self._lock:
                    self._active[plugin_id] = candidate
                    self._failed.pop(plugin_id, None)
                self.store.append_audit(
                    PluginAuditEvent(
                        action="activate",
                        plugin_id=plugin_id,
                        package_digest=installed.package_digest,
                        detail=candidate.record.generation_id,
                    )
                )
                return "active"
            finally:
                self._end_catalog_mutation()

    async def disable(self, plugin_id: str, *, wait: bool = True) -> PluginStatus:
        if not wait:
            raise ValueError("non-blocking plugin disable is not supported")
        self._set_desired(plugin_id, False, reject_dependents=True)
        return await self._deactivate(plugin_id, reason="disable")

    async def suspend(self, plugin_id: str, *, revoke: bool = False) -> PluginStatus:
        """Withdraw an active generation without changing desired state."""

        return await self._deactivate(
            plugin_id,
            reason="restart",
            withdraw_before_drain=revoke,
        )

    async def enable(self, plugin_id: str) -> PluginStatus:
        self._set_desired(plugin_id, True)
        try:
            graph = resolve_plugin_dependencies(self.store.read_state(), roots={plugin_id})
        except PluginDependencyError as exc:
            self._record_dependency_failure(plugin_id, exc)
            return "incompatible"
        status: PluginStatus = "ready"
        for node in graph.nodes:
            self._set_desired(node.plugin_id, True)
            status = self.status(node.plugin_id)
            if status != "active":
                status = await self.activate(node.plugin_id)
            if node.plugin_id != plugin_id and status != "active":
                return "incompatible"
        return status

    async def restart(self, plugin_id: str) -> PluginStatus:
        record = self._plugin_record(plugin_id)
        if not record.activation.desired_enabled:
            return "disabled"
        return await self.activate(plugin_id)

    def snapshot(self) -> ExtensionCatalogSnapshot:
        if self._catalog_mutating:
            raise RuntimeError("plugin catalog is changing; retry the run")
        leases: list[PluginGenerationLease] = []
        try:
            for plugin_id in sorted(self._active):
                leases.append(self._active[plugin_id].acquire())
        except Exception:
            for lease in leases:
                lease.close()
            raise
        self._acquire_catalog_snapshot()
        return ExtensionCatalogSnapshot(
            tuple(leases),
            on_close=self._release_catalog_snapshot,
        )

    def snapshot_for(
        self,
        expected_generations: list[dict[str, object]],
    ) -> ExtensionCatalogSnapshot:
        if self._catalog_mutating:
            raise RuntimeError("plugin catalog is changing; retry the run")
        leases: list[PluginGenerationLease] = []
        try:
            expected_ids = {str(item.get("plugin_id", "")) for item in expected_generations}
            if len(expected_ids) != len(expected_generations) or "" in expected_ids:
                raise RuntimeError("required plugin catalog metadata is invalid")
            if expected_ids != set(self._active):
                raise RuntimeError("required plugin catalog is no longer active")
            for expected in expected_generations:
                plugin_id = str(expected.get("plugin_id", ""))
                active = self._active.get(plugin_id)
                if active is None:
                    raise RuntimeError(f"required plugin generation is unavailable: {plugin_id}")
                if active.installed.package_digest != expected.get("package_digest"):
                    raise RuntimeError(f"required plugin digest is unavailable: {plugin_id}")
                if active.installed.version != expected.get("version"):
                    raise RuntimeError(f"required plugin version is unavailable: {plugin_id}")
                if active.record.generation_id != expected.get("generation_id"):
                    raise RuntimeError(f"required plugin generation is unavailable: {plugin_id}")
                expected_hashes = expected.get("contract_hashes", {})
                if expected_hashes != active.record.contract_hashes:
                    raise RuntimeError(f"plugin contribution contract changed: {plugin_id}")
                leases.append(active.acquire())
        except Exception:
            for lease in leases:
                lease.close()
            raise
        self._acquire_catalog_snapshot()
        return ExtensionCatalogSnapshot(
            tuple(leases),
            on_close=self._release_catalog_snapshot,
        )

    def project_status(self, record: PluginRecord) -> PluginStatus:
        installed = record.selected()
        if installed is None:
            return "invalid"
        if not record.activation.desired_enabled:
            return "disabled"
        package_root = self.store.package_path(record.plugin_id, installed.package_digest)
        if configuration_errors(installed, record.activation, package_root):
            return "needs_configuration"
        if not code_trust_is_valid(installed, record.activation):
            return "needs_code_trust"
        if not capability_grant_is_valid(installed, record.activation):
            return "needs_authorization"
        if record.plugin_id in self._active:
            return "active"
        if self._failed.get(record.plugin_id) == installed.package_digest:
            return "failed"
        return "ready"

    def status(self, plugin_id: str) -> PluginStatus:
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
        if plugin_id is not None:
            values = [item for item in values if item.plugin_id == plugin_id]
        return list(values)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._catalog_mutating = True
        async with self._lock:
            generations = [*self._active.values(), *self._retired.values()]
            for generation in generations:
                generation.draining = True
                if generation.lease_count == 0:
                    generation.drained.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._catalog_drained.wait(),
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

    async def _deactivate(
        self,
        plugin_id: str,
        *,
        reason: StopReason,
        withdraw_before_drain: bool = False,
    ) -> PluginStatus:
        async with self._mutation_lock:
            async with self._lock:
                generation = self._active.get(plugin_id)
                if generation is None:
                    return self.status(plugin_id)
                self._begin_catalog_mutation()
                generation.draining = True
                self._retired[generation.record.generation_id] = generation
                if generation.lease_count == 0:
                    generation.drained.set()
                if withdraw_before_drain:
                    generation.contribution_lease.close()
            try:
                await self._catalog_drained.wait()
                await generation.drained.wait()
                await self._stop_generation(generation, reason=reason)
                async with self._lock:
                    if self._active.get(plugin_id) is generation:
                        self._active.pop(plugin_id, None)
                self.store.append_audit(
                    PluginAuditEvent(
                        action=reason,
                        plugin_id=plugin_id,
                        package_digest=generation.installed.package_digest,
                    )
                )
                return self.status(plugin_id)
            finally:
                self._end_catalog_mutation()

    def _begin_catalog_mutation(self) -> None:
        self._catalog_mutating = True
        if self._catalog_lease_count:
            self._catalog_drained.clear()

    def _end_catalog_mutation(self) -> None:
        if not self._closed:
            self._catalog_mutating = False

    def _acquire_catalog_snapshot(self) -> None:
        self._catalog_lease_count += 1
        self._catalog_drained.clear()

    def _release_catalog_snapshot(self) -> None:
        if self._catalog_lease_count:
            self._catalog_lease_count -= 1
        if self._catalog_lease_count == 0:
            self._catalog_drained.set()

    def _record_dependency_failure(
        self,
        plugin_id: str,
        exc: PluginDependencyError,
    ) -> None:
        self._diagnostics.append(
            PluginDiagnostic(
                code="plugin_dependency_resolution_failed",
                phase="dependencies",
                message=redact_text(str(exc)),
                plugin_id=plugin_id,
                suggested_action="Install and select compatible plugin dependencies.",
            )
        )

    async def _build_generation(
        self,
        installed: InstalledPlugin,
        state: PluginRecord,
    ) -> ActivePluginGeneration:
        generation = PluginGenerationRecord(
            generation_id=new_id("plg"),
            plugin_id=installed.plugin_id,
            version=installed.version,
            package_digest=installed.package_digest,
            runtime_kind=installed.manifest.runtime.kind,
            status="starting",
        )
        diagnostics: list[PluginDiagnostic] = []
        redactor = SensitiveValueRedactor()

        def task_failure(exc: Exception) -> None:
            diagnostics.append(
                PluginDiagnostic(
                    code="plugin_background_task_failed",
                    phase="background",
                    message=f"{type(exc).__name__}: {redactor.redact(str(exc))}",
                    plugin_id=installed.plugin_id,
                    version=installed.version,
                    package_digest=installed.package_digest,
                    generation_id=generation.generation_id,
                )
            )

        tasks = OwnedTaskGroup(task_failure)
        module: LoadedPluginModule | None = None
        resources: list[Any] = []
        instance: Any = _DeclarativePluginInstance()
        contribution_lease: ContributionLease | None = None
        prepared: PreparedContributionSet | None = None
        started_services: list[Any] = []
        package_root = self.store.verify_package(
            installed.plugin_id,
            installed.package_digest,
        )
        configuration = state.activation.configuration_for(installed.package_digest)
        try:
            if installed.manifest.runtime.kind == "python-inprocess":
                entrypoint = installed.manifest.runtime.entrypoint
                assert entrypoint is not None
                factory, module = load_plugin_factory(package_root, entrypoint)
                context = build_plugin_context(
                    installed,
                    generation,
                    data_root=self.store.data_root,
                    configuration=configuration.values,
                    credential_bindings=configuration.credential_refs,
                    credential_resolver=self.credential_resolver,
                    tasks=tasks,
                    diagnostics=diagnostics,
                    redactor=redactor,
                )
                instance = factory(context)
                if inspect.isawaitable(instance):
                    instance = await instance
            elif installed.manifest.runtime.kind == "worker":

                def worker_diagnostic(code: str, message: str) -> None:
                    diagnostics.append(
                        PluginDiagnostic(
                            code=code,
                            phase="worker",
                            severity="warning",
                            message=redactor.redact(message),
                            plugin_id=installed.plugin_id,
                            version=installed.version,
                            package_digest=installed.package_digest,
                            generation_id=generation.generation_id,
                        )
                    )

                credentials = _resolve_worker_credentials(
                    configuration.credential_refs,
                    self.credential_resolver,
                )
                for value in credentials.values():
                    redactor.add(value)
                instance = WorkerPluginInstance(
                    installed.manifest,
                    package_root=package_root,
                    package_digest=installed.package_digest,
                    permissions=frozenset(request.id for request in installed.manifest.permissions),
                    configuration=configuration.values,
                    data_directory=self.store.data_root / installed.plugin_id,
                    credentials=credentials,
                    diagnostic=worker_diagnostic,
                )
            _validate_instance(instance)
            contributions = await instance.prepare()
            if isinstance(contributions, sdk.ContributionSet):
                contributions = adapt_sdk_contributions(contributions)
            if not isinstance(contributions, PluginContributions):
                raise TypeError("plugin prepare() must return SDK ContributionSet")
            discovered_tools: tuple[Tool, ...] = ()
            if any(item.auto_start for item in installed.manifest.contributes.mcp_servers):
                mcp_resource = PluginMCPResource(
                    installed.manifest,
                    package_root,
                    resolve_credential=self.credential_resolver,
                    credential_bindings=configuration.credential_refs,
                )
                resources.append(mcp_resource)
                discovered_tools = await mcp_resource.start()
            prepared = self.coordinator.prepare(
                installed,
                generation,
                contributions,
                static_hooks=_static_skill_hooks(
                    installed,
                    generation,
                    package_root=package_root,
                ),
                discovered_tools=discovered_tools,
            )
            generation.contract_hashes = dict(prepared.contract_hashes)
            contribution_lease = self.coordinator.commit(prepared)
            await instance.start()
            for service in prepared.background_services:
                await service.start()
                started_services.append(service)
            generation.status = "active"
            active = ActivePluginGeneration(
                record=generation,
                installed=installed,
                instance=instance,
                contributions=prepared,
                contribution_lease=contribution_lease,
                tasks=tasks,
                module=module,
                resources=resources,
                diagnostics=diagnostics,
            )
            return active
        except BaseException as exc:
            if contribution_lease is not None:
                contribution_lease.close()
            with suppress(Exception):
                await _stop_instance(
                    instance,
                    reason="startup_failed",
                    timeout=self.stop_timeout_seconds,
                )
            for service in reversed(started_services):
                with suppress(Exception):
                    await service.stop()
            await tasks.close(timeout=self.stop_timeout_seconds)
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
        if generation.stopped:
            return
        generation.stopped = True
        errors: list[Exception] = []
        try:
            await _stop_instance(
                generation.instance,
                reason=reason,
                timeout=self.stop_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - continue owner cleanup.
            errors.append(exc)
        for service in reversed(generation.contributions.background_services):
            try:
                await service.stop()
            except Exception as exc:  # noqa: BLE001 - continue owner cleanup.
                errors.append(exc)
        await generation.tasks.close(timeout=self.stop_timeout_seconds)
        for resource in reversed(generation.resources):
            try:
                await resource.close()
            except Exception as exc:  # noqa: BLE001 - continue owner cleanup.
                errors.append(exc)
        generation.contribution_lease.close()
        if generation.module is not None:
            generation.module.close()
        generation.record.status = "disabled"
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

    def _set_desired(
        self,
        plugin_id: str,
        enabled: bool,
        *,
        reject_dependents: bool = False,
    ) -> None:
        def update(state):
            try:
                record = state.plugins[plugin_id]
            except KeyError as exc:
                raise KeyError(f"plugin is not installed: {plugin_id}") from exc
            if reject_dependents:
                dependents = active_dependents(state, plugin_id)
                if dependents:
                    raise PluginDependencyError(
                        f"plugin has active dependents: {plugin_id}: {', '.join(dependents)}"
                    )
            record.activation.desired_enabled = enabled
            return state

        self.store.mutate_state(update)

    def _record_failure(self, installed: InstalledPlugin, phase: str, exc: Exception) -> None:
        diagnostic = PluginDiagnostic(
            code="plugin_activation_failed",
            phase=phase,
            message=f"{type(exc).__name__}: {redact_text(str(exc))}",
            plugin_id=installed.plugin_id,
            version=installed.version,
            package_digest=installed.package_digest,
            retryable=True,
            suggested_action="Review plugin diagnostics and retry activation.",
        )
        self._diagnostics.append(diagnostic)
        self._failed[installed.plugin_id] = installed.package_digest
        self.store.append_audit(
            PluginAuditEvent(
                action="activation",
                plugin_id=installed.plugin_id,
                package_digest=installed.package_digest,
                result="failed",
                detail=diagnostic.message,
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


async def _stop_instance(instance: Any, *, reason: StopReason, timeout: float) -> None:
    deadline = monotonic() + timeout
    await asyncio.wait_for(instance.stop(reason, deadline), timeout=timeout)


def _static_skill_hooks(
    installed: InstalledPlugin,
    generation: PluginGenerationRecord,
    *,
    package_root: Path,
) -> tuple[HookRegistration, ...]:
    hooks: list[HookRegistration] = []
    for declaration in installed.manifest.contributes.skills:
        declared_path = resolve_manifest_path(
            package_root,
            declaration.path,
            allow_directory=True,
        )
        root = declared_path.parent if declared_path.is_file() else declared_path
        catalog = SkillLoader(root, default_source="plugin").load_catalog()
        if not catalog.skills:
            if declaration.optional:
                continue
            detail = "; ".join(item.message for item in catalog.diagnostics)
            suffix = f": {detail}" if detail else ""
            raise ValueError(f"plugin skill contribution is empty: {declaration.id}{suffix}")
        prompt = render_agent_skills_prompt(catalog.skills)

        def add_skill_context(_context, *, text=prompt) -> HookResult:
            return HookResult(added_context=[text])

        hooks.append(
            HookRegistration(
                plugin_id=installed.plugin_id,
                hook_name="after_context_build",
                handler=add_skill_context,
                source="third_party",
                priority=90,
                timeout_seconds=1.0,
                hook_id=f"skill:{declaration.id}",
                generation_id=generation.generation_id,
            )
        )
    return tuple(hooks)


def _resolve_worker_credentials(
    bindings: dict[str, str],
    resolver: Callable[[str], str] | None,
) -> dict[str, str]:
    if not bindings:
        return {}
    if resolver is None:
        raise RuntimeError("plugin credential resolver is unavailable")
    return {slot: resolver(reference) for slot, reference in bindings.items()}
