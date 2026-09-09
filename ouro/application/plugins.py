from __future__ import annotations

import builtins
from pathlib import Path

from ouro.application.dto import Diagnostic
from ouro.application.plugin_commands import execute_plugin_command
from ouro.application.plugin_models import (
    CompatibilityItemView,
    CompatibilityReportView,
    PluginActionResult,
    PluginCommandArgumentView,
    PluginCommandExecutionView,
    PluginCommandView,
    PluginDetail,
    PluginInstallView,
    PluginProvenanceView,
    PluginScaffoldView,
    PluginSummary,
    PluginValidationView,
)
from ouro.application.session_controls import SessionControlCoordinator
from ouro.application.usage import UsageService
from ouro.core.redaction import redact_text
from ouro.core.session_controls import AttributedSessionControlTransition
from ouro.plugins.builtin import BuiltinPluginDefinition, BuiltinPluginRegistry
from ouro.plugins.catalog import ExtensionCatalogLease
from ouro.plugins.compatibility import inspect_community_package
from ouro.plugins.dependencies import (
    PluginDependencyError,
    active_dependents,
    builtin_desired_enabled,
    desired_dependant_closure,
    resolve_plugin_dependencies,
)
from ouro.plugins.installer import PluginInstaller
from ouro.plugins.manifest import PluginStatus
from ouro.plugins.models import (
    InstalledPlugin,
    PluginAuditEvent,
    PluginClass,
    PluginDiagnostic,
    PluginRecord,
    PluginStateDocument,
    create_configuration_revision,
)
from ouro.plugins.package import PluginPackageError
from ouro.plugins.readiness import configuration_errors, plugin_install_readiness
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.scaffold import create_plugin_scaffold
from ouro.plugins.store import PluginPackageStore
from ouro.sessions import SessionStore

_PLUGIN_GENERATION_RETAINING_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_user",
        "waiting_approval",
        "waiting_child",
        "paused",
        "recoverable",
    }
)


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
            plugin_id: self._summary(record)
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
            with self.runtime.snapshot() as snapshot:
                generations = [
                    generation.model_dump(mode="json", exclude_none=True)
                    for generation in snapshot.generations
                    if generation.plugin_id == plugin_id
                ]
        return PluginDetail(
            **summary.model_dump(),
            description=manifest.description,
            namespace=manifest.namespace,
            installs=[self._install_view(item, record) for item in record.installs],
            capabilities=sorted(manifest.contributes.capabilities()),
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
            configuration_supported=manifest.configuration is not None,
            configuration_values=(dict(configuration.values) if configuration is not None else {}),
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
                        ("builtin_contract_hash", item.builtin_contract_hash),
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
        snapshot = self._require_runtime().snapshot()
        try:
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
                for item in snapshot.commands.list()
            ]
        finally:
            snapshot.close()

    async def execute_command(
        self,
        command_id: str,
        argument: str = "",
        *,
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> PluginCommandExecutionView:
        runtime = self._require_runtime()
        snapshot = await self._command_catalog(runtime, session_id=session_id)
        try:
            return await execute_plugin_command(
                catalog=snapshot,
                store=self.session_store,
                command_id=command_id,
                argument=argument,
                session_id=session_id,
                workspace=workspace,
                usage_service=self.usage_service,
            )
        finally:
            snapshot.close()

    async def _command_catalog(
        self,
        runtime: PluginRuntimeResource,
        *,
        session_id: str | None,
    ) -> ExtensionCatalogLease:
        if session_id is None or self.session_store is None:
            return runtime.snapshot()
        session = self.session_store.load_session_record(session_id)
        active_run_id = session.active_run_id
        if active_run_id is None:
            return runtime.snapshot()
        try:
            active_run = self.session_store.load_run(session_id, active_run_id)
        except FileNotFoundError:
            return runtime.snapshot()
        if active_run.status not in {
            "queued",
            "running",
            "waiting_user",
            "waiting_approval",
            "waiting_child",
        }:
            return runtime.snapshot()
        generations = active_run.metadata.get("extension_generations", [])
        if not isinstance(generations, list) or any(
            not isinstance(item, dict) for item in generations
        ):
            raise ValueError("active run has invalid extension generation metadata")
        raw_catalog_id = active_run.metadata.get("extension_catalog_id")
        catalog_id = raw_catalog_id if isinstance(raw_catalog_id, str) else None
        return await runtime.snapshot_for(generations, catalog_id=catalog_id)

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

    async def configure(
        self,
        plugin_id: str,
        values: dict[str, object],
        *,
        credential_refs: dict[str, str] | None = None,
        package_digest: str | None = None,
        request_id: str | None = None,
        preview: bool = False,
    ) -> PluginActionResult:
        self._require_user_operation(plugin_id, "configure")
        expected = self.store.read_state()
        record = _state_plugin_record(expected, plugin_id)
        installed = _select_install(record, package_digest)
        previous_configuration = record.activation.configuration_for(installed.package_digest)
        if previous_configuration is None:
            raise PluginPackageError("plugin has no configuration revision for this digest")
        candidate_activation = record.activation.model_copy(deep=True)
        next_credential_refs = (
            previous_configuration.credential_refs if credential_refs is None else credential_refs
        )
        if any(not reference.startswith("env:") for reference in next_credential_refs.values()):
            raise ValueError("plugin credential bindings must use env: references")
        specification = installed.manifest.configuration
        configuration = create_configuration_revision(
            values=values,
            credential_refs=dict(next_credential_refs),
            schema_version=specification.version if specification is not None else None,
        )
        candidate_activation.set_configuration(
            installed.package_digest,
            configuration,
            activate=candidate_activation.selected_digest == installed.package_digest,
        )
        errors = configuration_errors(
            installed,
            candidate_activation,
            self.store.package_path(plugin_id, installed.package_digest),
        )
        if errors:
            raise PluginPackageError("invalid plugin configuration: " + "; ".join(errors))
        if preview:
            return PluginActionResult(
                action="configure",
                plugin_id=plugin_id,
                status=self._status(record),
                changed=False,
                package_digest=installed.package_digest,
            )
        rebuild_active = (
            record.activation.selected_digest == installed.package_digest
            and self.runtime is not None
            and self.runtime.status(plugin_id) in {"active", "degraded"}
        )
        candidate = expected.model_copy(deep=True)
        candidate.plugins[plugin_id].activation = candidate_activation
        if rebuild_active:
            assert self.runtime is not None
            await self.runtime.apply_candidate_state(
                expected=expected,
                candidate=candidate,
                roots={plugin_id},
                reason="configure",
            )
        else:
            self.store.commit_candidate_state(expected, candidate)
        self._audit("configure", installed, request_id)
        return self._action("configure", plugin_id, installed.package_digest)

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
        activation.active_configuration_revision_id = configuration.revision_id
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

    async def disable(self, plugin_id: str) -> PluginActionResult:
        runtime = self._require_runtime()
        await self._release_plugin_session_controls(plugin_id)
        status = await runtime.disable(plugin_id)
        return PluginActionResult(action="disable", plugin_id=plugin_id, status=status)

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
        if purge_data and any(
            install.package_digest != target_digest for install in record.installs
        ):
            raise ValueError(
                "plugin data can only be purged while removing the final installed digest"
            )
        references = self._durable_references(plugin_id, target_digest)
        if references:
            raise ValueError(
                "plugin digest is retained by active durable runs: " + ", ".join(references)
            )
        removing_selected = target_digest == record.activation.selected_digest
        dependents = (
            active_dependents(
                self.store.read_state(),
                plugin_id,
                builtin_contracts=self.builtin_contracts,
            )
            if removing_selected
            else []
        )
        if dependents:
            raise ValueError(f"plugin has active dependents: {plugin_id}: {', '.join(dependents)}")
        if removing_selected and record.activation.desired_enabled:
            await self._require_runtime().disable(plugin_id)
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
        )

    def gc(self) -> builtins.list[str]:
        return [str(path) for path in self.installer.garbage_collect()]

    def purge_data(self, plugin_id: str) -> PluginActionResult:
        definition = self.builtin_plugins.get(plugin_id)
        if definition is not None:
            state = self.store.read_state()
            if builtin_desired_enabled(state, definition.dependency_contract()):
                raise ValueError(f"plugin must be disabled before purging data: {plugin_id}")
            references = self._durable_references(
                plugin_id,
                definition.contract_hash,
                plugin_class="builtin",
            )
            if references:
                raise ValueError(
                    "plugin data is retained by active durable runs: " + ", ".join(references)
                )
            self.store.purge_data(plugin_id)
            return PluginActionResult(
                action="purge_data",
                plugin_id=plugin_id,
                status="disabled",
            )
        record = self._record(plugin_id)
        if record.activation.desired_enabled:
            raise ValueError(f"plugin must be disabled before purging data: {plugin_id}")
        references = [
            reference
            for install in record.installs
            for reference in self._durable_references(plugin_id, install.package_digest)
        ]
        if references:
            raise ValueError(
                "plugin data is retained by active durable runs: " + ", ".join(references)
            )
        self.store.purge_data(plugin_id)
        return PluginActionResult(action="purge_data", plugin_id=plugin_id, status="disabled")

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

    def _summary(self, record: PluginRecord) -> PluginSummary:
        installed = _require_selected(record)
        return PluginSummary(
            plugin_id=installed.plugin_id,
            name=installed.manifest.name,
            version=installed.version,
            package_digest=installed.package_digest,
            status=self._status(record),
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
            version=definition.version,
            builtin_contract_hash=definition.contract_hash,
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
            with self.runtime.snapshot() as snapshot:
                generations = [
                    generation.model_dump(mode="json", exclude_none=True)
                    for generation in snapshot.generations
                    if generation.plugin_id == definition.plugin_id
                ]
        return PluginDetail(
            **self._builtin_summary(definition, state).model_dump(),
            description=manifest.description,
            namespace=manifest.namespace,
            installs=[],
            capabilities=sorted(manifest.contributes.capabilities()),
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
            return self.runtime.status(definition.plugin_id)
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

    def _status(self, record: PluginRecord) -> PluginStatus:
        if self.runtime is not None:
            return self.runtime.project_status(record)
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

    def _audit(
        self,
        action: str,
        installed: InstalledPlugin,
        request_id: str | None = None,
    ) -> None:
        self.store.append_audit(
            PluginAuditEvent(
                action=action,
                plugin_id=installed.plugin_id,
                package_digest=installed.package_digest,
                request_id=request_id,
            )
        )

    def _require_runtime(self) -> PluginRuntimeResource:
        if self.runtime is None:
            raise RuntimeError("plugin runtime lifecycle is unavailable")
        return self.runtime

    async def _release_plugin_session_controls(self, plugin_id: str) -> None:
        if self.session_store is None or self.session_controls is None or self.runtime is None:
            return
        lease = self.runtime.snapshot()
        try:
            for session in self.session_store.list_sessions():
                activations = [
                    activation
                    for activation in session.controls.activations
                    if activation.plugin_id == plugin_id
                ]
                for activation in activations:
                    try:
                        await self.session_controls.apply(
                            AttributedSessionControlTransition(
                                action="release",
                                activation_id=activation.activation_id,
                                plugin_id=activation.plugin_id,
                                generation_id=activation.generation_id,
                                execution_contract_hash=(activation.execution_contract_hash),
                                reason="plugin_disable",
                            ),
                            session_id=session.session_id,
                            controllers=lease.session_controllers,
                            workspace=session.workspace,
                        )
                    except Exception:  # noqa: BLE001 - disable must revoke Host gates.
                        self.session_controls.force_release(
                            session_id=session.session_id,
                            activation_id=activation.activation_id,
                            reason="plugin_disable_cleanup_unavailable",
                        )
        finally:
            lease.close()

    def _durable_references(
        self,
        plugin_id: str,
        release_id: str | None,
        *,
        plugin_class: PluginClass = "user",
    ) -> builtins.list[str]:
        if self.session_store is None or release_id is None:
            return []
        references: builtins.list[str] = []
        for session in self.session_store.list_sessions():
            # Only Run records carry execution-contract references; Session journals
            # are never read here.
            for run in self.session_store.load_runs(session.session_id):
                if run.status not in _PLUGIN_GENERATION_RETAINING_RUN_STATUSES:
                    continue
                generations = run.metadata.get("extension_generations", [])
                if not isinstance(generations, list):
                    continue
                if any(
                    isinstance(item, dict)
                    and item.get("plugin_id") == plugin_id
                    and item.get("plugin_class", "user") == plugin_class
                    and (
                        item.get("release_id") == release_id
                        or (
                            plugin_class == "user"
                            and "release_id" not in item
                            and item.get("package_digest") == release_id
                        )
                    )
                    for item in generations
                ):
                    references.append(f"{session.session_id}/{run.run_id}")
        return references

    def _reject_durable_references(
        self,
        plugin_id: str,
        package_digest: str,
        *,
        action: str,
    ) -> None:
        references = self._durable_references(plugin_id, package_digest)
        if references:
            raise ValueError(
                f"cannot {action} plugin contract retained by active durable runs: "
                + ", ".join(references)
            )


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
