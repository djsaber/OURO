from __future__ import annotations

import builtins
from pathlib import Path

from ouro.application.plugin_models import (
    CompatibilityItemView,
    CompatibilityReportView,
    PluginActionResult,
    PluginCommandExecutionView,
    PluginCommandView,
    PluginDetail,
    PluginInstallView,
    PluginProvenanceView,
    PluginSummary,
    PluginValidationView,
)
from ouro.core.redaction import redact_text
from ouro.plugins.authorization import (
    capability_grant_is_valid,
    code_trust_is_valid,
    configuration_errors,
    create_capability_grant,
    create_code_trust_grant,
)
from ouro.plugins.compatibility import inspect_community_package
from ouro.plugins.dependencies import active_dependents, resolve_plugin_dependencies
from ouro.plugins.installer import PluginInstaller
from ouro.plugins.manifest import PluginStatus
from ouro.plugins.models import (
    InstalledPlugin,
    PluginAuditEvent,
    PluginDiagnostic,
    PluginRecord,
    PluginScope,
    PluginStateDocument,
)
from ouro.plugins.package import PluginPackageError
from ouro.plugins.registry import PluginCommandRegistry
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.sessions import SessionStore

_PLUGIN_GENERATION_RETAINING_RUN_STATUSES = frozenset(
    {"queued", "running", "waiting_user", "waiting_approval", "paused", "recoverable"}
)


class PluginService:
    """Host-neutral plugin queries and state-changing use cases."""

    def __init__(
        self,
        store: PluginPackageStore,
        *,
        runtime: PluginRuntimeResource | None = None,
        commands: PluginCommandRegistry | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.store = store
        self.installer = PluginInstaller(store)
        self.runtime = runtime
        self.commands = commands
        self.session_store = session_store

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
            legacy=package.legacy,
            file_count=package.file_count,
            total_bytes=package.total_bytes,
        )

    def install(
        self,
        source: str | Path,
        *,
        ref: str | None = None,
        subdirectory: str | None = None,
        scope: PluginScope = "user",
        request_id: str | None = None,
        preview: bool = False,
    ) -> PluginActionResult | PluginValidationView:
        if preview:
            return self.validate(source, ref=ref, subdirectory=subdirectory)
        result = self.installer.install(
            source,
            ref=ref,
            subdirectory=subdirectory,
            scope=scope,
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
        return [
            self._summary(record)
            for _, record in sorted(state.plugins.items(), key=lambda item: item[0])
            if record.selected() is not None
        ]

    def show(self, plugin_id: str) -> PluginDetail:
        record = self._record(plugin_id)
        installed = _require_selected(record)
        summary = self._summary(record)
        manifest = installed.manifest
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
        )

    def diagnostics(self, plugin_id: str | None = None) -> builtins.list[PluginDiagnostic]:
        return self.runtime.diagnostics(plugin_id) if self.runtime is not None else []

    def list_commands(self) -> builtins.list[PluginCommandView]:
        registry = self._require_commands()
        return [
            PluginCommandView(
                command_id=item.command_id,
                plugin_id=item.plugin_id,
                name=item.name,
                description=item.description,
            )
            for item in registry.list()
        ]

    async def execute_command(
        self,
        command_id: str,
        argument: str = "",
        *,
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> PluginCommandExecutionView:
        registry = self._require_commands()
        command = registry.get(command_id)
        runtime = self._require_runtime()
        snapshot = runtime.snapshot()
        try:
            if not any(item.plugin_id == command.plugin_id for item in snapshot.generations):
                raise RuntimeError(f"plugin command owner is not active: {command.plugin_id}")
            result = await registry.execute(
                command_id,
                argument,
                session_id=session_id,
                workspace=workspace,
            )
        finally:
            snapshot.close()
        return PluginCommandExecutionView(
            command_id=result.command_id,
            output=result.output,
            data=dict(result.data),
            session_inputs=list(result.attributed_session_inputs),
        )

    def dependencies(
        self,
        plugin_id: str,
    ) -> builtins.list[dict[str, str | None]]:
        installed = _require_selected(self._record(plugin_id))
        return [
            item.model_dump(mode="json", exclude_none=True)
            for item in installed.manifest.dependencies.plugins
        ]

    def import_compatibility(self, source: str | Path) -> CompatibilityReportView:
        report = inspect_community_package(source)
        return CompatibilityReportView(
            source_format=report.source_format,
            source_version=report.source_version,
            items=[
                CompatibilityItemView(
                    kind=item.kind,
                    identity=item.identity,
                    outcome=item.outcome,
                    reason=item.reason,
                )
                for item in report.items
            ],
            security_differences=report.security_differences,
            required_permissions=report.required_permissions,
            suggested_target=report.suggested_target,
        )

    def authorize(
        self,
        plugin_id: str,
        *,
        package_digest: str | None = None,
        request_id: str | None = None,
        preview: bool = False,
    ) -> PluginActionResult:
        record = self._record(plugin_id)
        installed = _select_install(record, package_digest)
        grant = create_capability_grant(installed, scope=record.activation.scope)
        if preview:
            return PluginActionResult(
                action="authorize",
                plugin_id=plugin_id,
                status=self._status(record),
                changed=False,
                package_digest=installed.package_digest,
            )

        def update(state: PluginStateDocument) -> PluginStateDocument:
            activation = state.plugins[plugin_id].activation
            activation.capability_grants = [
                item
                for item in activation.capability_grants
                if not (
                    item.permission_fingerprint == grant.permission_fingerprint
                    and item.contribution_fingerprint == grant.contribution_fingerprint
                    and item.runtime_kind == grant.runtime_kind
                    and item.scope == grant.scope
                )
            ]
            activation.capability_grants.append(grant)
            return state

        self.store.mutate_state(update)
        self._audit("authorize", installed, request_id=request_id)
        return self._action("authorize", plugin_id, installed.package_digest)

    def trust_code(
        self,
        plugin_id: str,
        *,
        package_digest: str | None = None,
        request_id: str | None = None,
        preview: bool = False,
    ) -> PluginActionResult:
        record = self._record(plugin_id)
        installed = _select_install(record, package_digest)
        grant = create_code_trust_grant(installed, scope=record.activation.scope)
        if preview:
            return PluginActionResult(
                action="trust_code",
                plugin_id=plugin_id,
                status=self._status(record),
                changed=False,
                package_digest=installed.package_digest,
            )

        def update(state: PluginStateDocument) -> PluginStateDocument:
            grants = state.plugins[plugin_id].activation.code_trust_grants
            if grant not in grants:
                grants.append(grant)
            return state

        self.store.mutate_state(update)
        self._audit("trust_code", installed, request_id=request_id)
        return self._action("trust_code", plugin_id, installed.package_digest)

    async def revoke(
        self,
        plugin_id: str,
        *,
        code_trust: bool = False,
        request_id: str | None = None,
    ) -> PluginActionResult:
        record = self._record(plugin_id)
        installed = _require_selected(record)
        if self.runtime is not None and self.runtime.status(plugin_id) == "active":
            # Security revocation removes registry projections before waiting
            # for in-flight generation leases to drain.
            await self.runtime.suspend(plugin_id, revoke=True)

        def update(state: PluginStateDocument) -> PluginStateDocument:
            activation = state.plugins[plugin_id].activation
            if code_trust:
                activation.code_trust_grants = []
            else:
                activation.capability_grants = []
            return state

        self.store.mutate_state(update)
        self._audit("revoke_code_trust" if code_trust else "revoke", installed, request_id)
        return self._action("revoke", plugin_id, installed.package_digest)

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
        record = self._record(plugin_id)
        installed = _select_install(record, package_digest)
        previous_configuration = record.activation.configuration_for(
            installed.package_digest
        ).model_copy(deep=True)
        candidate = record.activation.model_copy(deep=True)
        configuration = candidate.configuration_for(installed.package_digest).model_copy(deep=True)
        configuration.values = dict(values)
        if installed.manifest.configuration is not None:
            configuration.schema_version = installed.manifest.configuration.version
        if credential_refs is not None:
            if any(not reference.startswith("env:") for reference in credential_refs.values()):
                raise ValueError("plugin credential bindings must use env: references")
            configuration.credential_refs = dict(credential_refs)
        candidate.configurations[installed.package_digest] = configuration
        candidate.configuration = configuration
        errors = configuration_errors(
            installed,
            candidate,
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
        was_active = (
            record.activation.selected_digest == installed.package_digest
            and self.runtime is not None
            and self.runtime.status(plugin_id) == "active"
        )
        if was_active:
            self._reject_durable_references(
                plugin_id,
                installed.package_digest,
                action="reconfigure",
            )

        def update(state: PluginStateDocument) -> PluginStateDocument:
            activation = state.plugins[plugin_id].activation
            current = activation.configuration_for(installed.package_digest)
            if current != previous_configuration:
                raise ValueError(
                    "plugin configuration conflict: configuration changed concurrently"
                )
            activation.configurations[installed.package_digest] = configuration
            if activation.selected_digest == installed.package_digest:
                activation.configuration = configuration
            return state

        self.store.mutate_state(update)
        self._audit("configure", installed, request_id)
        if was_active:
            assert self.runtime is not None
            status = await self.runtime.restart(plugin_id)
            if status != "active":

                def restore(state: PluginStateDocument) -> PluginStateDocument:
                    activation = state.plugins[plugin_id].activation
                    current = activation.configuration_for(installed.package_digest)
                    if current != configuration:
                        raise ValueError(
                            "plugin configuration changed while restoring failed activation"
                        )
                    activation.configurations[installed.package_digest] = previous_configuration
                    if activation.selected_digest == installed.package_digest:
                        activation.configuration = previous_configuration
                    return state

                self.store.mutate_state(restore)
                restored = await self.runtime.activate(plugin_id)
                if restored != "active":
                    raise RuntimeError(
                        "plugin configuration activation failed and the previous "
                        "generation could not be restored"
                    )
                raise RuntimeError(
                    "plugin configuration activation failed; the previous generation was restored"
                )
        return self._action("configure", plugin_id, installed.package_digest)

    async def select(
        self,
        plugin_id: str,
        package_digest: str,
        *,
        expected_digest: str | None = None,
    ) -> PluginActionResult:
        record = self._record(plugin_id)
        installed = _select_install(record, package_digest)
        previous_digest = record.activation.selected_digest
        changed = record.activation.selected_digest != package_digest
        was_active = self.runtime is not None and self.runtime.status(plugin_id) == "active"
        if changed and previous_digest is not None:
            self._reject_durable_references(
                plugin_id,
                previous_digest,
                action="replace",
            )
        if record.activation.desired_enabled and self.runtime is None:
            raise RuntimeError(
                "cannot change the selected digest of an enabled plugin without its runtime"
            )
        if record.activation.desired_enabled and changed:
            package_root = self.store.package_path(plugin_id, installed.package_digest)
            if configuration_errors(installed, record.activation, package_root):
                raise PluginPackageError("selected plugin digest needs configuration")
            if not code_trust_is_valid(installed, record.activation):
                raise PermissionError("selected plugin digest needs code trust")
            if not capability_grant_is_valid(installed, record.activation):
                raise PermissionError("selected plugin digest needs capability authorization")

        def update(state: PluginStateDocument) -> PluginStateDocument:
            activation = state.plugins[plugin_id].activation
            expected = expected_digest if expected_digest is not None else previous_digest
            if activation.selected_digest != expected:
                raise ValueError(
                    f"plugin selection conflict: expected {expected}, "
                    f"found {activation.selected_digest}"
                )
            activation.selected_digest = package_digest
            activation.configuration = activation.configuration_for(package_digest)
            if activation.desired_enabled:
                # Selection is the lockfile mutation boundary.  Validate the
                # complete desired graph before committing bytes that could
                # invalidate an already active dependant.
                resolve_plugin_dependencies(state)
            return state

        self.store.mutate_state(update)
        if was_active and changed:
            assert self.runtime is not None
            status = await self.runtime.activate(plugin_id)
            if status != "active":
                assert previous_digest is not None

                def restore(state: PluginStateDocument) -> PluginStateDocument:
                    activation = state.plugins[plugin_id].activation
                    if activation.selected_digest != package_digest:
                        raise ValueError(
                            "plugin selection changed while restoring a failed generation"
                        )
                    activation.selected_digest = previous_digest
                    activation.configuration = activation.configuration_for(previous_digest)
                    resolve_plugin_dependencies(state)
                    return state

                self.store.mutate_state(restore)
                restored = await self.runtime.activate(plugin_id)
                if restored != "active":
                    raise RuntimeError(
                        "plugin generation activation failed and the previous generation "
                        "could not be restored"
                    )
                raise RuntimeError(
                    "plugin generation activation failed; the previous generation was restored"
                )
        return self._action("select", plugin_id, installed.package_digest)

    async def enable(self, plugin_id: str) -> PluginActionResult:
        runtime = self._require_runtime()
        status = await runtime.enable(plugin_id)
        return PluginActionResult(action="enable", plugin_id=plugin_id, status=status)

    async def disable(self, plugin_id: str) -> PluginActionResult:
        runtime = self._require_runtime()
        record = self._record(plugin_id)
        installed = _require_selected(record)
        if runtime.status(plugin_id) == "active":
            self._reject_durable_references(
                plugin_id,
                installed.package_digest,
                action="disable",
            )
        status = await runtime.disable(plugin_id)
        return PluginActionResult(action="disable", plugin_id=plugin_id, status=status)

    async def restart(self, plugin_id: str) -> PluginActionResult:
        runtime = self._require_runtime()
        installed = _require_selected(self._record(plugin_id))
        if runtime.status(plugin_id) == "active":
            self._reject_durable_references(
                plugin_id,
                installed.package_digest,
                action="restart",
            )
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
        current = _require_selected(self._record(plugin_id))
        result = self.installer.install(
            source,
            ref=ref,
            subdirectory=subdirectory,
            scope=current.provenance.scope,
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

    async def rollback(self, plugin_id: str, package_digest: str) -> PluginActionResult:
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
            active_dependents(self.store.read_state(), plugin_id) if removing_selected else []
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
            scope=record.activation.scope,
        )

    def _status(self, record: PluginRecord) -> PluginStatus:
        if self.runtime is not None:
            return self.runtime.project_status(record)
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
        return "ready"

    def _record(self, plugin_id: str) -> PluginRecord:
        try:
            return self.store.read_state().plugins[plugin_id]
        except KeyError as exc:
            raise KeyError(f"plugin is not installed: {plugin_id}") from exc

    def _install_view(self, installed: InstalledPlugin, record: PluginRecord) -> PluginInstallView:
        provenance = installed.provenance
        return PluginInstallView(
            plugin_id=installed.plugin_id,
            name=installed.manifest.name,
            version=installed.version,
            package_digest=installed.package_digest,
            runtime_kind=installed.manifest.runtime.kind,
            scope=provenance.scope,
            selected=record.activation.selected_digest == installed.package_digest,
            legacy=installed.legacy,
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

    def _require_commands(self) -> PluginCommandRegistry:
        if self.commands is None:
            raise RuntimeError("plugin command registry is unavailable")
        return self.commands

    def _durable_references(
        self,
        plugin_id: str,
        package_digest: str | None,
    ) -> builtins.list[str]:
        if self.session_store is None or package_digest is None:
            return []
        references: builtins.list[str] = []
        for session in self.session_store.list_sessions():
            # Only Run records carry generation references; Session journals
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
                    and item.get("package_digest") == package_digest
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
                f"cannot {action} plugin generation retained by active durable runs: "
                + ", ".join(references)
            )


def _require_selected(record: PluginRecord) -> InstalledPlugin:
    installed = record.selected()
    if installed is None:
        raise ValueError(f"plugin has no selected install: {record.plugin_id}")
    return installed


def _select_install(record: PluginRecord, digest: str | None) -> InstalledPlugin:
    if digest is None:
        return _require_selected(record)
    try:
        return next(item for item in record.installs if item.package_digest == digest)
    except StopIteration as exc:
        raise ValueError(f"plugin digest is not installed: {record.plugin_id}@{digest}") from exc
