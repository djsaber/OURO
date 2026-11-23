from __future__ import annotations

import builtins
from base64 import b64encode
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Literal

from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.plugin_configuration import (
    PluginConfigurationCandidate,
    PluginConfigurationPreview,
    PluginConfigurationResult,
    PluginConfigurationView,
)
from ouro.application.contracts.plugins import (
    CompatibilityItemView,
    CompatibilityReportView,
    PluginActionResult,
    PluginCommandArgumentView,
    PluginCommandExecutionView,
    PluginCommandView,
    PluginDetail,
    PluginInstallView,
    PluginMaintenanceResult,
    PluginProvenanceView,
    PluginScaffoldView,
    PluginSummary,
    PluginValidationView,
)
from ouro.application.harness import capture_session_harness
from ouro.application.plugins.commands import execute_plugin_command
from ouro.application.plugins.configuration import PluginConfigurationService
from ouro.application.runs.recovery import retained_plugin_releases
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.usage import UsageService
from ouro.core.callbacks import run_blocking
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.redaction import redact_text
from ouro.core.session_ports import SessionStore
from ouro.core.state import SessionRecord
from ouro.local_processes.commands import run_process
from ouro.plugins.builtin import BuiltinPluginDefinition, BuiltinPluginRegistry
from ouro.plugins.compatibility import inspect_community_package
from ouro.plugins.configuration.service import declared_credential_slots
from ouro.plugins.dependencies import (
    PluginDependencyError,
    builtin_desired_enabled,
    desired_dependant_closure,
    resolve_plugin_dependencies,
)
from ouro.plugins.installation.icons import read_plugin_icon
from ouro.plugins.installation.installer import PluginInstaller
from ouro.plugins.installation.paths import PluginPackageError
from ouro.plugins.installation.scaffold import create_plugin_scaffold
from ouro.plugins.installation.store import PluginPackageStore
from ouro.plugins.lifecycle.readiness import configuration_errors, plugin_install_readiness
from ouro.plugins.lifecycle.runtime import PluginRuntimeResource
from ouro.plugins.manifest import PluginStatus, validate_plugin_id
from ouro.plugins.models import (
    InstalledPlugin,
    PluginAuditEvent,
    PluginClass,
    PluginDiagnostic,
    PluginRecord,
    PluginStateDocument,
)
from ouro.plugins.semver import SemVer


class PluginOperationNotAllowedError(ValueError):
    pass


class PluginService:
    """Host-neutral plugin queries and state-changing use cases."""

    def __init__(
        self,
        store: PluginPackageStore,
        *,
        runtime: PluginRuntimeResource | None = None,
        session_store: SessionStore | None = None,
        builtin_plugins: BuiltinPluginRegistry | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        self.store = store
        self.builtin_plugins = builtin_plugins or BuiltinPluginRegistry()
        self.builtin_contracts = self.builtin_plugins.contracts
        if runtime is not None and runtime.builtin_contracts != self.builtin_contracts:
            raise ValueError("plugin service and runtime builtin registries must match")
        self.installer = PluginInstaller(
            store,
            builtin_plugins=self.builtin_plugins,
        )
        self.runtime = runtime
        self._configuration = PluginConfigurationService(store, runtime)
        self.session_store = session_store
        self.usage_service = usage_service
        self.session_controls = (
            SessionControlCoordinator(session_store, usage_service=usage_service)
            if session_store is not None
            else None
        )

    def validate(
        self,
        source: str | Path,
        *,
        ref: str | None = None,
        subdirectory: str | None = None,
    ) -> PluginValidationView:
        try:
            package = self.installer.validate(
                source,
                ref=ref,
                subdirectory=subdirectory,
            )
        except Exception as exc:  # noqa: BLE001 - public static validation boundary.
            return PluginValidationView(
                valid=False,
                diagnostics=[
                    PluginDiagnostic(
                        code="plugin_validation_failed",
                        phase="validate",
                        message=f"{type(exc).__name__}: {redact_text(str(exc))}",
                        suggested_action="Fix the package and validate it again.",
                    )
                ],
            )
        return PluginValidationView(
            valid=True,
            plugin_id=package.manifest.plugin_id,
            name=package.manifest.name,
            version=package.manifest.version,
            package_digest=package.package_digest,
            runtime_kind=package.manifest.runtime.kind,
            file_count=package.file_count,
            total_bytes=package.total_bytes,
        )

    def scaffold(
        self,
        destination: str | Path,
        *,
        plugin_id: str,
        name: str,
        namespace: str | None = None,
    ) -> PluginScaffoldView:
        result = create_plugin_scaffold(
            destination,
            plugin_id=plugin_id,
            name=name,
            namespace=namespace,
        )
        manifest = result.package.manifest
        return PluginScaffoldView(
            root=str(result.root),
            plugin_id=manifest.plugin_id,
            name=manifest.name,
            version=manifest.version,
            namespace=manifest.namespace,
            runtime_kind=manifest.runtime.kind,
            package_digest=result.package.package_digest,
            files=list(result.files),
        )

    def install(
        self,
        source: str | Path,
        *,
        ref: str | None = None,
        subdirectory: str | None = None,
        request_id: str | None = None,
        preview: bool = False,
    ) -> PluginActionResult | PluginValidationView:
        if preview:
            return self.validate(source, ref=ref, subdirectory=subdirectory)
        result = self.installer.install(
            source,
            ref=ref,
            subdirectory=subdirectory,
            request_id=request_id,
        )
        return PluginActionResult(
            action="install",
            plugin_id=result.installed.plugin_id,
            status="disabled",
            changed=not result.already_installed,
            package_digest=result.installed.package_digest,
            diagnostics=list(result.diagnostics),
        )

    def list(self) -> list[PluginSummary]:
        state = self.store.read_state()
        summaries = {
            plugin_id: self._summary(record, state=state)
            for plugin_id, record in state.plugins.items()
            if record.selected() is not None
        }
        summaries.update(
            {
                definition.plugin_id: self._builtin_summary(definition, state)
                for definition in self.builtin_plugins.list()
            }
        )
        return sorted(
            summaries.values(),
            key=lambda summary: (
                summary.plugin_class == "builtin",
                summary.plugin_id,
            ),
        )

    def show(self, plugin_id: str) -> PluginDetail:
        definition = self.builtin_plugins.get(plugin_id)
        if definition is not None:
            return self._builtin_detail(definition)
        record = self._record(plugin_id)
        installed = _require_selected(record)
        summary = self._summary(record)
        manifest = installed.manifest
        configuration = record.activation.configuration_for(installed.package_digest)
        generations: list[dict[str, object]] = []
        if self.runtime is not None:
            generations = [
                generation.model_dump(mode="json", exclude_none=True)
                for generation in self.runtime.catalog().generations
                if generation.plugin_id == plugin_id
            ]
        return PluginDetail(
            **summary.model_dump(),
            description=manifest.description,
            namespace=manifest.namespace,
            installs=[
                self._install_view(item, record)
                for item in sorted(
                    record.installs,
                    key=lambda item: (SemVer.parse(item.manifest.version), item.package_digest),
                    reverse=True,
                )
            ],
            capabilities=sorted(manifest.capabilities()),
            exports=[item.model_dump(mode="json") for item in manifest.exports],
            permissions=[
                item.model_dump(mode="json", exclude_none=True) for item in manifest.permissions
            ],
            contributions={
                kind: [item.id for item in items]
                for kind, items in manifest.contributes.by_kind().items()
                if items
            },
            dependencies=[
                item.model_dump(mode="json", exclude_none=True)
                for item in manifest.dependencies.plugins
            ],
            active_generations=generations,
            diagnostics=(self.runtime.diagnostics(plugin_id) if self.runtime is not None else []),
            configuration_supported=(
                (
                    manifest.configuration is not None
                    and manifest.configuration.schema_path is not None
                )
                or bool(declared_credential_slots(manifest))
            ),
            configuration_values=(configuration.values_dict() if configuration is not None else {}),
            credential_refs=(
                dict(configuration.credential_refs) if configuration is not None else {}
            ),
        )

    def diagnostics(self, plugin_id: str | None = None) -> builtins.list[PluginDiagnostic]:
        return self.runtime.diagnostics(plugin_id) if self.runtime is not None else []

    def startup_diagnostics(self) -> tuple[Diagnostic, ...]:
        """Project plugin startup failures into the Host-neutral diagnostic contract."""

        return tuple(
            Diagnostic(
                code=item.code,
                severity=item.severity,
                component="plugins",
                message=item.message,
                plugin_id=item.plugin_id,
                occurred_at=item.created_at,
                recoverable=item.retryable or item.code == "plugin_state_reset",
                suggested_action=item.suggested_action,
                metadata={
                    key: value
                    for key, value in (
                        ("phase", item.phase),
                        ("version", item.version),
                        ("plugin_class", item.plugin_class),
                        ("release_id", item.release_id),
                        ("package_digest", item.package_digest),
                        ("generation_id", item.generation_id),
                        ("contribution_id", item.contribution_id),
                        ("correlation_id", item.correlation_id),
                    )
                    if value is not None
                },
            )
            for item in self.diagnostics()
            if item.phase == "startup"
        )

    def list_commands(self) -> builtins.list[PluginCommandView]:
        catalog = self._require_runtime().catalog()
        return [
            PluginCommandView(
                command_id=item.command_id,
                plugin_id=item.plugin_id,
                name=item.name,
                description=item.description,
                usage=item.usage,
                argument_options=tuple(
                    PluginCommandArgumentView(
                        value=option.value,
                        description=option.description,
                        usage=option.usage,
                    )
                    for option in item.argument_options
                ),
                requires_session=item.requires_session,
            )
            for item in catalog.commands.list()
        ]

    async def execute_command(
        self,
        command_id: str,
        argument: str = "",
        *,
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> PluginCommandExecutionView:
        runtime = self._require_runtime()
        await runtime.synchronize()
        captured_harness = None
        if session_id is not None and self.session_store is not None:
            before, draft = await run_blocking(self._capture_command_session, session_id)
            captured_harness = (before, draft)
        controller_ids = (
            {item.controller_id for item in captured_harness[0].controls.activations}
            if captured_harness is not None
            else set()
        )
        binding = runtime.catalog().bind_command(command_id, controller_ids=controller_ids)
        try:
            commands, controllers = binding.value
            return await execute_plugin_command(
                commands=commands,
                controllers=controllers,
                store=self.session_store,
                command_id=command_id,
                argument=argument,
                session_id=session_id,
                workspace=workspace,
                usage_service=self.usage_service,
                captured_harness=captured_harness,
                select_driver=runtime.select_driver,
            )
        finally:
            binding.close()

    def _capture_command_session(
        self, session_id: str
    ) -> tuple[SessionRecord, HarnessCallSnapshot]:
        assert self.session_store is not None
        return capture_session_harness(
            self.session_store, session_id, usage_service=self.usage_service
        )

    def dependencies(
        self,
        plugin_id: str,
    ) -> builtins.list[dict[str, str | None]]:
        definition = self.builtin_plugins.get(plugin_id)
        manifest = (
            definition.manifest
            if definition is not None
            else _require_selected(self._record(plugin_id)).manifest
        )
        return [
            item.model_dump(mode="json", exclude_none=True)
            for item in manifest.dependencies.plugins
        ]

    def import_compatibility(self, source: str | Path) -> CompatibilityReportView:
        report = inspect_community_package(source)
        outcome_counts = {
            outcome: sum(item.outcome == outcome for item in report.items)
            for outcome in ("supported", "converted", "ignored", "incompatible")
        }
        return CompatibilityReportView(
            source_format=report.source_format,
            source_version=report.source_version,
            detected_resources=report.detected_resources,
            items=[
                CompatibilityItemView(
                    kind=item.kind,
                    identity=item.identity,
                    outcome=item.outcome,
                    reason=item.reason,
                    ouro_target=item.ouro_target,
                )
                for item in report.items
            ],
            outcome_counts=outcome_counts,
            lost_semantics=report.lost_semantics,
            security_differences=report.security_differences,
            required_permissions=report.required_permissions,
            suggested_target=report.suggested_target,
        )

    def configuration(
        self, plugin_id: str, *, package_digest: str | None = None
    ) -> PluginConfigurationView:
        self._require_user_operation(plugin_id, "configure")
        return self._configuration.view(plugin_id, package_digest)

    def preview_configuration(
        self, candidate: PluginConfigurationCandidate
    ) -> PluginConfigurationPreview:
        self._require_user_operation(candidate.plugin_id, "configure")
        return self._configuration.preview(candidate)

    async def configure(
        self,
        candidate: PluginConfigurationCandidate,
        *,
        request_id: str | None = None,
    ) -> PluginConfigurationResult:
        self._require_user_operation(candidate.plugin_id, "configure")
        return await self._configuration.apply(candidate, request_id=request_id)

    async def select(
        self,
        plugin_id: str,
        package_digest: str,
        *,
        expected_digest: str | None = None,
    ) -> PluginActionResult:
        self._require_user_operation(plugin_id, "select a release for")
        expected = self.store.read_state()
        record = _state_plugin_record(expected, plugin_id)
        installed = _select_install(record, package_digest)
        previous_digest = record.activation.selected_digest
        changed = record.activation.selected_digest != package_digest
        if record.activation.desired_enabled and self.runtime is None:
            raise RuntimeError(
                "cannot change the selected digest of an enabled plugin without its runtime"
            )
        if record.activation.desired_enabled and changed:
            package_root = self.store.package_path(plugin_id, installed.package_digest)
            if configuration_errors(installed, record.activation, package_root):
                raise PluginPackageError("selected plugin digest needs configuration")

        selected_expected = expected_digest if expected_digest is not None else previous_digest
        if previous_digest != selected_expected:
            raise ValueError(
                f"plugin selection conflict: expected {selected_expected}, found {previous_digest}"
            )
        if not changed:
            return self._action("select", plugin_id, installed.package_digest)
        candidate = expected.model_copy(deep=True)
        activation = candidate.plugins[plugin_id].activation
        activation.selected_digest = package_digest
        configuration = activation.configuration_for(package_digest)
        if configuration is None:
            raise ValueError("selected plugin digest has no configuration revision")
        if activation.desired_enabled:
            affected = desired_dependant_closure(
                candidate,
                {plugin_id},
                builtin_contracts=self.builtin_contracts,
            )
            resolve_plugin_dependencies(
                candidate,
                roots=affected,
                builtin_contracts=self.builtin_contracts,
            )
            assert self.runtime is not None
            await self.runtime.apply_candidate_state(
                expected=expected,
                candidate=candidate,
                roots={plugin_id},
                reason="select",
            )
        else:
            self.store.commit_candidate_state(expected, candidate)
        return self._action("select", plugin_id, installed.package_digest)

    async def enable(self, plugin_id: str) -> PluginActionResult:
        runtime = self._require_runtime()
        status = await runtime.enable(plugin_id)
        return PluginActionResult(action="enable", plugin_id=plugin_id, status=status)

    async def disable(self, plugin_id: str, *, wait: bool = True) -> PluginActionResult:
        status = await self._disable(plugin_id, wait=wait)
        return PluginActionResult(action="disable", plugin_id=plugin_id, status=status)

    async def _disable(
        self, plugin_id: str, *, wait: bool = True, selected_digest: str | None = None
    ) -> PluginStatus:
        return await self._require_runtime().disable(
            plugin_id,
            wait=wait,
            selected_digest=selected_digest,
            release_controls=self.session_controls.release_plugin
            if self.session_controls
            else None,
        )

    async def restart(self, plugin_id: str) -> PluginActionResult:
        runtime = self._require_runtime()
        status = await runtime.restart(plugin_id)
        return PluginActionResult(action="restart", plugin_id=plugin_id, status=status)

    async def update(
        self,
        plugin_id: str,
        source: str | Path,
        *,
        ref: str | None = None,
        subdirectory: str | None = None,
        request_id: str | None = None,
    ) -> PluginActionResult:
        self._require_user_operation(plugin_id, "update")
        _require_selected(self._record(plugin_id))
        result = self.installer.install(
            source,
            ref=ref,
            subdirectory=subdirectory,
            request_id=request_id,
            expected_plugin_id=plugin_id,
        )
        return PluginActionResult(
            action="update",
            plugin_id=plugin_id,
            status=self._status(self._record(plugin_id)),
            package_digest=result.installed.package_digest,
            diagnostics=list(result.diagnostics),
        )

    async def reload(
        self,
        plugin_id: str,
        source: str | Path,
        *,
        ref: str | None = None,
        subdirectory: str | None = None,
        request_id: str | None = None,
    ) -> PluginActionResult:
        """Install local development bytes and atomically select them when ready."""

        self._require_user_operation(plugin_id, "reload")
        before = self._record(plugin_id)
        previous_digest = before.activation.selected_digest
        desired_enabled = before.activation.desired_enabled
        installed_result = self.installer.install(
            source,
            ref=ref,
            subdirectory=subdirectory,
            request_id=request_id,
            expected_plugin_id=plugin_id,
        )
        installed = installed_result.installed
        record = self._record(plugin_id)
        candidate_status = self._install_readiness(record, installed)
        if desired_enabled and candidate_status != "ready":
            return PluginActionResult(
                action="reload",
                plugin_id=plugin_id,
                status=candidate_status,
                changed=not installed_result.already_installed,
                package_digest=installed.package_digest,
                diagnostics=[
                    PluginDiagnostic(
                        code="plugin_reload_candidate_not_ready",
                        phase="reload",
                        severity="warning",
                        message=f"plugin reload candidate is {candidate_status}",
                        plugin_id=plugin_id,
                        version=installed.version,
                        package_digest=installed.package_digest,
                        suggested_action=(
                            "Configure or explicitly grant the candidate, then reload again."
                        ),
                    )
                ],
            )
        selected = await self.select(plugin_id, installed.package_digest)
        if desired_enabled and previous_digest == installed.package_digest:
            selected = await self.restart(plugin_id)
        return PluginActionResult(
            action="reload",
            plugin_id=plugin_id,
            status=selected.status,
            changed=(
                not installed_result.already_installed
                or previous_digest != installed.package_digest
            ),
            package_digest=installed.package_digest,
            diagnostics=list(installed_result.diagnostics),
        )

    async def rollback(self, plugin_id: str, package_digest: str) -> PluginActionResult:
        self._require_user_operation(plugin_id, "roll back")
        selected = await self.select(plugin_id, package_digest)
        return PluginActionResult(
            action="rollback",
            plugin_id=plugin_id,
            status=selected.status,
            package_digest=package_digest,
        )

    async def remove(
        self,
        plugin_id: str,
        *,
        package_digest: str | None = None,
        purge_data: bool = False,
        request_id: str | None = None,
    ) -> PluginActionResult:
        self._require_user_operation(plugin_id, "remove")
        record = self._record(plugin_id)
        target_digest = package_digest or record.activation.selected_digest
        if target_digest is None:
            raise ValueError(f"plugin has no selected install: {plugin_id}")
        target = _select_install(record, target_digest)
        target_digest = target.package_digest
        if purge_data:
            record.require_final_install(target_digest)
        references = self._durable_references(plugin_id, target_digest)
        if purge_data and references:
            raise ValueError(
                "plugin digest is retained by active durable runs: " + ", ".join(references)
            )
        removing_selected = target_digest == record.activation.selected_digest
        if removing_selected and (
            record.activation.desired_enabled
            or (self.runtime is not None and (self.runtime.has_resources(plugin_id) or references))
        ):
            status = await self._disable(plugin_id, selected_digest=target_digest)
            if status != "disabled":
                raise ValueError(f"plugin resources must stop before removal: {plugin_id}")
        if self.runtime is not None and self.runtime.has_resources(
            plugin_id, package_digest=target_digest
        ):
            raise ValueError(f"plugin resources must stop before removal: {plugin_id}")
        with self.store.data_lock(plugin_id, exclusive=True) if purge_data else nullcontext():
            if purge_data:
                self._require_inactive_data(plugin_id)
            self.installer.remove_install(
                plugin_id,
                package_digest=target_digest,
                purge_data=purge_data,
                request_id=request_id,
            )
        remaining = self.store.read_state().plugins.get(plugin_id)
        return PluginActionResult(
            action="remove",
            plugin_id=plugin_id,
            status=self._status(remaining) if remaining is not None else "disabled",
            package_digest=target_digest,
            retained_by=references,
        )

    def gc(self) -> builtins.list[str]:
        referenced = {
            (plugin_id, release_id)
            for plugin_class, plugin_id, release_id in retained_plugin_releases(self.session_store)
            if plugin_class == "user"
        }
        return [str(path) for path in self.store.garbage_collect(referenced)]

    def purge_data(self, plugin_id: str) -> PluginActionResult:
        plugin_id = validate_plugin_id(plugin_id)
        if self.runtime is not None and self.runtime.has_resources(plugin_id):
            raise ValueError(f"plugin resources must stop before purging data: {plugin_id}")
        with self.store.data_lock(plugin_id, exclusive=True):
            self._require_inactive_data(plugin_id)
            self.store.purge_data(plugin_id)
            self.store.append_audit(
                PluginAuditEvent(
                    action="purge_data",
                    plugin_id=plugin_id,
                    detail="private data purged",
                )
            )
            return PluginActionResult(action="purge_data", plugin_id=plugin_id, status="disabled")

    async def maintain_data(
        self,
        plugin_id: str,
        arguments: Sequence[str],
        *,
        timeout_seconds: float = 300,
    ) -> PluginMaintenanceResult:
        """Run an explicit owner command with exclusive access to disabled private data."""
        plugin_id = validate_plugin_id(plugin_id)
        with self.store.data_lock(plugin_id, exclusive=True):
            await run_blocking(self._require_inactive_data, plugin_id)
            directory = self.store.data_root / plugin_id
            if not directory.is_dir():
                raise ValueError(f"plugin data directory does not exist: {plugin_id}")
            try:
                result = await run_process(
                    arguments, cwd=directory, timeout_seconds=timeout_seconds
                )
            except BaseException:
                await run_blocking(
                    self.store.append_audit,
                    PluginAuditEvent(
                        action="maintain_data",
                        plugin_id=plugin_id,
                        result="failed",
                        detail="command did not complete",
                    ),
                )
                raise
            status: Literal["succeeded", "failed"] = (
                "succeeded" if result.returncode == 0 and not result.timed_out else "failed"
            )
            await run_blocking(
                self.store.append_audit,
                PluginAuditEvent(
                    action="maintain_data",
                    plugin_id=plugin_id,
                    result=status,
                    detail=f"{status}; exit={result.returncode}; timed_out={result.timed_out}",
                ),
            )
            return PluginMaintenanceResult(
                plugin_id=plugin_id,
                status=status,
                returncode=result.returncode,
                timed_out=result.timed_out,
                stdout=result.stdout,
                stderr=result.stderr,
                stdout_truncated=result.stdout_truncated,
                stderr_truncated=result.stderr_truncated,
            )

    def _require_inactive_data(self, plugin_id: str) -> None:
        """Check under an exclusive data lock, which also prevents new enablement."""
        definition = self.builtin_plugins.get(plugin_id)
        state = self.store.read_state()
        if definition is not None:
            if builtin_desired_enabled(state, definition.dependency_contract()):
                raise ValueError(f"plugin must be disabled before data maintenance: {plugin_id}")
            references = self._durable_references(
                plugin_id,
                None,
                plugin_class="builtin",
            )
        else:
            record = state.plugins.get(plugin_id)
            if record is not None and record.activation.desired_enabled:
                raise ValueError(f"plugin must be disabled before data maintenance: {plugin_id}")
            references = self._durable_references(plugin_id, None)
        if references:
            raise ValueError(
                "plugin data is retained by active durable runs: " + ", ".join(references)
            )

    def check(self, plugin_id: str) -> PluginActionResult:
        definition = self.builtin_plugins.get(plugin_id)
        if definition is not None:
            return PluginActionResult(
                action="check",
                plugin_id=plugin_id,
                status=self._builtin_status(definition, self.store.read_state()),
                changed=False,
            )
        record = self._record(plugin_id)
        installed = _require_selected(record)
        diagnostics = self.diagnostics(plugin_id)
        status: PluginStatus
        try:
            self.store.verify_package(plugin_id, installed.package_digest)
        except Exception as exc:  # noqa: BLE001 - integrity report boundary.
            diagnostics.append(
                PluginDiagnostic(
                    code="plugin_package_integrity_failed",
                    phase="check",
                    message=f"{type(exc).__name__}: {redact_text(str(exc))}",
                    plugin_id=plugin_id,
                    version=installed.version,
                    package_digest=installed.package_digest,
                    suggested_action="Reinstall the plugin from a trusted source.",
                )
            )
            status = "invalid"
        else:
            status = self._status(record)
        return PluginActionResult(
            action="check",
            plugin_id=plugin_id,
            status=status,
            changed=False,
            package_digest=installed.package_digest,
            diagnostics=diagnostics,
        )

    def _summary(
        self, record: PluginRecord, *, state: PluginStateDocument | None = None
    ) -> PluginSummary:
        installed = _require_selected(record)
        return PluginSummary(
            plugin_id=installed.plugin_id,
            name=installed.manifest.name,
            author=installed.manifest.author,
            icon=_icon_data_url(
                self.store.package_path(installed.plugin_id, installed.package_digest),
                installed.manifest.icon,
            ),
            version=installed.version,
            package_digest=installed.package_digest,
            status=self._status(record, state=state),
            runtime_kind=installed.manifest.runtime.kind,
            desired_enabled=record.activation.desired_enabled,
            plugin_class="user",
        )

    def _builtin_summary(
        self,
        definition: BuiltinPluginDefinition,
        state: PluginStateDocument,
    ) -> PluginSummary:
        return PluginSummary(
            plugin_id=definition.plugin_id,
            name=definition.manifest.name,
            author=definition.manifest.author,
            icon=_icon_data_url(definition.root, definition.manifest.icon),
            version=definition.version,
            status=self._builtin_status(definition, state),
            runtime_kind=definition.manifest.runtime.kind,
            desired_enabled=builtin_desired_enabled(state, definition.dependency_contract()),
            plugin_class="builtin",
            default_enabled=definition.default_enabled,
        )

    def _builtin_detail(self, definition: BuiltinPluginDefinition) -> PluginDetail:
        state = self.store.read_state()
        manifest = definition.manifest
        generations: list[dict[str, object]] = []
        if self.runtime is not None:
            generations = [
                generation.model_dump(mode="json", exclude_none=True)
                for generation in self.runtime.catalog().generations
                if generation.plugin_id == definition.plugin_id
            ]
        return PluginDetail(
            **self._builtin_summary(definition, state).model_dump(),
            description=manifest.description,
            namespace=manifest.namespace,
            installs=[],
            capabilities=sorted(manifest.capabilities()),
            exports=[item.model_dump(mode="json") for item in manifest.exports],
            permissions=[
                item.model_dump(mode="json", exclude_none=True) for item in manifest.permissions
            ],
            contributions={
                kind: [item.id for item in items]
                for kind, items in manifest.contributes.by_kind().items()
                if items
            },
            dependencies=[
                item.model_dump(mode="json", exclude_none=True)
                for item in manifest.dependencies.plugins
            ],
            active_generations=generations,
            diagnostics=(
                self.runtime.diagnostics(definition.plugin_id) if self.runtime is not None else []
            ),
        )

    def _builtin_status(
        self,
        definition: BuiltinPluginDefinition,
        state: PluginStateDocument,
    ) -> PluginStatus:
        if self.runtime is not None:
            return self.runtime.status(definition.plugin_id, state=state)
        if not builtin_desired_enabled(state, definition.dependency_contract()):
            return "disabled"
        try:
            resolve_plugin_dependencies(
                state,
                roots={definition.plugin_id},
                builtin_contracts=self.builtin_contracts,
            )
        except PluginDependencyError:
            return "incompatible"
        return "ready"

    def _status(
        self, record: PluginRecord, *, state: PluginStateDocument | None = None
    ) -> PluginStatus:
        if self.runtime is not None:
            return self.runtime.project_status(record, state=state)
        installed = record.selected()
        if installed is None:
            return "invalid"
        if not record.activation.desired_enabled:
            return "disabled"
        package_root = self.store.package_path(record.plugin_id, installed.package_digest)
        return plugin_install_readiness(installed, record.activation, package_root)

    def _install_readiness(
        self,
        record: PluginRecord,
        installed: InstalledPlugin,
    ) -> PluginStatus:
        package_root = self.store.package_path(record.plugin_id, installed.package_digest)
        return plugin_install_readiness(installed, record.activation, package_root)

    def _record(self, plugin_id: str) -> PluginRecord:
        try:
            return self.store.read_state().plugins[plugin_id]
        except KeyError as exc:
            raise KeyError(f"plugin is not installed: {plugin_id}") from exc

    def _require_user_operation(self, plugin_id: str, operation: str) -> None:
        if self.builtin_plugins.get(plugin_id) is not None:
            raise PluginOperationNotAllowedError(f"cannot {operation} builtin plugin: {plugin_id}")

    def _install_view(self, installed: InstalledPlugin, record: PluginRecord) -> PluginInstallView:
        provenance = installed.provenance
        return PluginInstallView(
            plugin_id=installed.plugin_id,
            name=installed.manifest.name,
            author=installed.manifest.author,
            icon=_icon_data_url(
                self.store.package_path(installed.plugin_id, installed.package_digest),
                installed.manifest.icon,
            ),
            version=installed.version,
            package_digest=installed.package_digest,
            runtime_kind=installed.manifest.runtime.kind,
            plugin_class="user",
            selected=record.activation.selected_digest == installed.package_digest,
            provenance=PluginProvenanceView(
                source_kind=provenance.source_kind,
                public_source=provenance.public_source,
                resolved_reference=provenance.resolved_reference,
            ),
        )

    def _action(self, action: str, plugin_id: str, package_digest: str) -> PluginActionResult:
        return PluginActionResult(
            action=action,
            plugin_id=plugin_id,
            status=self._status(self._record(plugin_id)),
            package_digest=package_digest,
        )

    def _require_runtime(self) -> PluginRuntimeResource:
        if self.runtime is None:
            raise RuntimeError("plugin runtime lifecycle is unavailable")
        return self.runtime

    def _durable_references(
        self,
        plugin_id: str,
        release_id: str | None,
        *,
        plugin_class: PluginClass = "user",
    ) -> builtins.list[str]:
        return sorted(
            {
                owner
                for (kind, identity, release), owners in retained_plugin_releases(
                    self.session_store
                ).items()
                if kind == plugin_class
                and identity == plugin_id
                and (release_id is None or release == release_id)
                for owner in owners
            }
        )


def _icon_data_url(root: Path, icon: str | None) -> str | None:
    try:
        content = read_plugin_icon(root, icon)
    except OSError, ValueError:
        return None
    return f"data:image/png;base64,{b64encode(content).decode('ascii')}" if content else None


def _require_selected(record: PluginRecord) -> InstalledPlugin:
    installed = record.selected()
    if installed is None:
        raise ValueError(f"plugin has no selected install: {record.plugin_id}")
    return installed


def _state_plugin_record(state: PluginStateDocument, plugin_id: str) -> PluginRecord:
    try:
        return state.plugins[plugin_id]
    except KeyError as exc:
        raise KeyError(f"plugin is not installed: {plugin_id}") from exc


def _select_install(record: PluginRecord, digest: str | None) -> InstalledPlugin:
    if digest is None:
        return _require_selected(record)
    try:
        return next(item for item in record.installs if item.package_digest == digest)
    except StopIteration as exc:
        raise ValueError(f"plugin digest is not installed: {record.plugin_id}@{digest}") from exc
