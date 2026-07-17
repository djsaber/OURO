from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass, field
from pathlib import Path

from ouro.plugins.models import (
    InstalledPlugin,
    PluginActivationState,
    PluginAuditEvent,
    PluginConfigurationRevision,
    PluginDiagnostic,
    PluginProvenance,
    PluginRecord,
    PluginStateDocument,
    create_configuration_revision,
)
from ouro.plugins.package import PackageLimits, validate_plugin_package
from ouro.plugins.sources import PluginSourceMaterializer, source_provider
from ouro.plugins.store import PluginPackageStore


@dataclass(frozen=True)
class PluginInstallResult:
    installed: InstalledPlugin
    diagnostics: tuple[PluginDiagnostic, ...] = field(default_factory=tuple)
    already_installed: bool = False


class PluginInstaller:
    """Resolve bytes and commit immutable package plus source-derived install facts."""

    def __init__(
        self,
        store: PluginPackageStore,
        *,
        limits: PackageLimits | None = None,
        builtin_plugin_ids: Set[str] = frozenset(),
    ) -> None:
        self.store = store
        self.limits = limits or PackageLimits()
        self.builtin_plugin_ids = frozenset(builtin_plugin_ids)

    def validate(
        self,
        source: str | Path,
        *,
        ref: str | None = None,
        subdirectory: str | None = None,
    ):
        provider = source_provider(source, ref=ref, subdirectory=subdirectory)
        with PluginSourceMaterializer(provider) as materialized:
            return validate_plugin_package(materialized.root, limits=self.limits)

    def install(
        self,
        source: str | Path,
        *,
        ref: str | None = None,
        subdirectory: str | None = None,
        request_id: str | None = None,
        expected_plugin_id: str | None = None,
    ) -> PluginInstallResult:
        provider = source_provider(source, ref=ref, subdirectory=subdirectory)
        with PluginSourceMaterializer(provider) as materialized:
            package = validate_plugin_package(materialized.root, limits=self.limits)
            if package.manifest.plugin_id in self.builtin_plugin_ids:
                raise ValueError(
                    f"user plugin cannot replace builtin identity: {package.manifest.plugin_id}"
                )
            if expected_plugin_id is not None and package.manifest.plugin_id != expected_plugin_id:
                raise ValueError(
                    f"plugin identity changed from {expected_plugin_id} "
                    f"to {package.manifest.plugin_id}"
                )
            self.store.publish(package)
            provenance = PluginProvenance(
                source_kind=materialized.source_kind,
                source_reference=materialized.source_reference,
                public_source=materialized.public_source,
                resolved_reference=materialized.resolved_reference,
                package_digest=package.package_digest,
            )
            installed = InstalledPlugin(
                plugin_id=package.manifest.plugin_id,
                version=package.manifest.version,
                package_digest=package.package_digest,
                manifest=package.manifest,
                provenance=provenance,
            )
            result = self._record_install(installed)
        self.store.append_audit(
            PluginAuditEvent(
                action="install",
                plugin_id=installed.plugin_id,
                package_digest=installed.package_digest,
                detail=(
                    "already installed" if result.already_installed else provenance.public_source
                ),
                request_id=request_id,
            )
        )
        return result

    def remove_install(
        self,
        plugin_id: str,
        *,
        package_digest: str | None = None,
        purge_data: bool = False,
        request_id: str | None = None,
    ) -> None:
        def remove(state: PluginStateDocument) -> PluginStateDocument:
            record = state.plugins.get(plugin_id)
            if record is None:
                return state
            digest = package_digest or record.activation.selected_digest
            if digest is None:
                return state
            if record.activation.desired_enabled and record.activation.selected_digest == digest:
                raise ValueError(f"plugin must be disabled before removal: {plugin_id}")
            record.installs = [item for item in record.installs if item.package_digest != digest]
            record.activation.remove_configuration_for(digest)
            if record.activation.selected_digest == digest:
                record.activation.selected_digest = (
                    record.installs[-1].package_digest if record.installs else None
                )
                if record.activation.selected_digest is not None:
                    revision = record.activation.configuration_for(
                        record.activation.selected_digest
                    )
                    if revision is None:
                        raise ValueError("selected plugin digest has no configuration revision")
                    record.activation.active_configuration_revision_id = revision.revision_id
                else:
                    record.activation.active_configuration_revision_id = None
                record.activation.capability_grants = []
            record.activation.code_trust_grants = [
                grant
                for grant in record.activation.code_trust_grants
                if grant.package_digest != digest
            ]
            if not record.installs:
                state.plugins.pop(plugin_id, None)
            return state

        self.store.mutate_state(remove)
        if purge_data:
            self.store.purge_data(plugin_id)
        self.store.append_audit(
            PluginAuditEvent(
                action="remove",
                plugin_id=plugin_id,
                package_digest=package_digest,
                detail="data purged" if purge_data else "data retained",
                request_id=request_id,
            )
        )

    def garbage_collect(self) -> list[Path]:
        state = self.store.read_state()
        referenced = {
            (plugin_id, installed.package_digest)
            for plugin_id, record in state.plugins.items()
            for installed in record.installs
        }
        return self.store.garbage_collect(referenced)

    def _record_install(self, installed: InstalledPlugin) -> PluginInstallResult:
        diagnostics: list[PluginDiagnostic] = []
        already_installed = False

        def add(state: PluginStateDocument) -> PluginStateDocument:
            nonlocal already_installed
            record = state.plugins.get(installed.plugin_id)
            if record is None:
                configuration = _initial_configuration_revision(installed)
                activation = PluginActivationState(selected_digest=installed.package_digest)
                activation.set_configuration(
                    installed.package_digest,
                    configuration,
                    activate=True,
                )
                record = PluginRecord(
                    plugin_id=installed.plugin_id,
                    activation=activation,
                )
                state.plugins[installed.plugin_id] = record
            existing = next(
                (
                    item
                    for item in record.installs
                    if item.package_digest == installed.package_digest
                ),
                None,
            )
            if existing is not None:
                already_installed = True
                return state
            conflicting = next(
                (item for item in record.installs if item.version == installed.version),
                None,
            )
            if conflicting is not None:
                diagnostics.append(
                    PluginDiagnostic(
                        code="version_digest_mismatch",
                        phase="install",
                        severity="warning",
                        message=(
                            f"version {installed.version} already exists with different content"
                        ),
                        plugin_id=installed.plugin_id,
                        version=installed.version,
                        package_digest=installed.package_digest,
                        suggested_action="Review provenance and explicitly select a digest.",
                    )
                )
            record.installs.append(installed)
            configuration = _initial_configuration_revision(installed)
            record.activation.set_configuration(
                installed.package_digest,
                configuration,
                activate=record.activation.selected_digest == installed.package_digest,
            )
            if record.activation.selected_digest is None:
                record.activation.selected_digest = installed.package_digest
                record.activation.active_configuration_revision_id = configuration.revision_id
            return state

        self.store.mutate_state(add)
        return PluginInstallResult(
            installed=installed,
            diagnostics=tuple(diagnostics),
            already_installed=already_installed,
        )


def _initial_configuration_revision(installed: InstalledPlugin) -> PluginConfigurationRevision:
    specification = installed.manifest.configuration
    return create_configuration_revision(
        schema_version=specification.version if specification is not None else None,
    )
