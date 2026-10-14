from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from pydantic import JsonValue

from ouro.application.plugin_configuration_models import (
    PluginConfigurationCandidate,
    PluginConfigurationPreview,
    PluginConfigurationResult,
    PluginConfigurationView,
    PluginCredentialSlotView,
)
from ouro.plugins.configuration import credential_binding_issues, declared_credential_slots
from ouro.plugins.configuration_schema import ConfigurationIssue, ConfigurationSchema
from ouro.plugins.credentials import PluginCredentialBinding
from ouro.plugins.models import (
    InstalledPlugin,
    PluginAuditEvent,
    PluginConfigurationRevision,
    PluginRecord,
    PluginStateDocument,
    configuration_revision_id,
    create_configuration_revision,
)
from ouro.plugins.package import PluginPackageError, load_configuration_schema
from ouro.plugins.readiness import plugin_install_readiness
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore, PluginStateConflictError


class PluginConfigurationValidationError(PluginPackageError):
    def __init__(self, issues: tuple[ConfigurationIssue, ...]) -> None:
        self.issues = issues
        super().__init__(
            "invalid plugin configuration: " + "; ".join(issue.message for issue in issues)
        )


class PluginConfigurationConflictError(PluginStateConflictError):
    def __init__(self, candidate: PluginConfigurationCandidate, current_revision_id: str) -> None:
        self.plugin_id = candidate.plugin_id
        self.package_digest = candidate.package_digest
        self.expected_revision_id = candidate.expected_configuration_revision_id
        self.current_revision_id = current_revision_id
        super().__init__("Plugin configuration changed; refresh the target and review your draft.")


@dataclass(frozen=True)
class _ConfigurationTarget:
    record: PluginRecord
    installed: InstalledPlugin
    revision: PluginConfigurationRevision
    schema: ConfigurationSchema | None

    @property
    def selected(self) -> bool:
        return self.record.activation.selected_digest == self.installed.package_digest

    @property
    def requires_runtime(self) -> bool:
        return self.selected and self.record.activation.desired_enabled

    @property
    def version(self) -> int | None:
        definition = self.installed.manifest.configuration
        return definition.version if definition is not None else None

    def issues(
        self, values: dict[str, object], refs: dict[str, PluginCredentialBinding]
    ) -> tuple[ConfigurationIssue, ...]:
        issues = credential_binding_issues(self.installed.manifest, refs)
        if self.schema is not None:
            return (*issues, *self.schema.validate(values))
        if values:
            return (
                *issues,
                ConfigurationIssue(
                    path="/values",
                    code="no_configuration_schema",
                    message="plugin without a configuration schema requires an empty configuration",
                ),
            )
        return issues


class PluginConfigurationService:
    """Accept full, version-bound drafts through the existing activation transaction."""

    def __init__(self, store: PluginPackageStore, runtime: PluginRuntimeResource | None) -> None:
        self.store = store
        self.runtime = runtime

    def view(self, plugin_id: str, package_digest: str | None = None) -> PluginConfigurationView:
        target = self._target(self.store.read_state(), plugin_id, package_digest)
        manifest = target.installed.manifest
        definition = manifest.configuration
        descriptions = definition.credentials if definition is not None else {}
        reasons = {
            slot: request.reason
            for request in manifest.permissions
            if request.id == "credentials.use"
            for slot in request.scopes
        }
        refs = dict(target.revision.credential_refs)
        values = target.revision.values_dict()
        issues = target.issues(values, refs)
        if target.revision.schema_version != target.version:
            issues = (
                *issues,
                ConfigurationIssue(
                    path="",
                    code="configuration_version",
                    message="Configuration version does not match the target package.",
                ),
            )
        slots = tuple(
            PluginCredentialSlotView(
                slot=slot,
                title=(descriptions[slot].title or slot) if slot in descriptions else slot,
                description=(descriptions[slot].description or reasons[slot])
                if slot in descriptions
                else reasons[slot],
                required=descriptions[slot].required if slot in descriptions else False,
                bound=slot in refs,
            )
            for slot in sorted(declared_credential_slots(manifest))
        )
        return PluginConfigurationView(
            plugin_id=plugin_id,
            name=manifest.name,
            package_digest=target.installed.package_digest,
            selected=target.selected,
            desired_enabled=target.record.activation.desired_enabled,
            configuration_supported=target.schema is not None or bool(slots),
            configuration_version=target.version,
            configuration_revision_id=target.revision.revision_id,
            schema_document=target.schema.document if target.schema is not None else None,
            values=cast(dict[str, JsonValue], values),
            credential_slots=slots,
            credential_refs=refs,
            issues=issues,
        )

    def preview(self, candidate: PluginConfigurationCandidate) -> PluginConfigurationPreview:
        candidate = PluginConfigurationCandidate.model_validate(
            candidate.model_dump(warnings=False)
        )
        expected = self.store.read_state()
        target = self._target(expected, candidate.plugin_id, candidate.package_digest)
        result = self._preview(target, candidate)
        if target.revision.revision_id != candidate.expected_configuration_revision_id:
            result.issues = (
                *result.issues,
                ConfigurationIssue(
                    path="/expected_configuration_revision_id",
                    code="configuration_conflict",
                    message=(
                        "Plugin configuration changed; refresh the target and review your draft."
                    ),
                ),
            )
            result.valid = False
        return result

    async def apply(
        self, candidate: PluginConfigurationCandidate, *, request_id: str | None = None
    ) -> PluginConfigurationResult:
        # Copy/revalidate at entry so caller edits cannot change a suspended operation.
        candidate = PluginConfigurationCandidate.model_validate(
            candidate.model_dump(warnings=False)
        )
        expected = self.store.read_state()
        target = self._target(expected, candidate.plugin_id, candidate.package_digest)
        if target.revision.revision_id != candidate.expected_configuration_revision_id:
            raise PluginConfigurationConflictError(candidate, target.revision.revision_id)
        preview = self._preview(target, candidate)
        if not preview.valid:
            raise PluginConfigurationValidationError(preview.issues)
        if not preview.changed:
            return self._result(target, target.revision, "unchanged", expected)
        if target.requires_runtime and self.runtime is None:
            raise RuntimeError(
                "Cannot apply configuration to an enabled selected plugin without its runtime."
            )
        self.store.verify_package(candidate.plugin_id, candidate.package_digest)
        revision = create_configuration_revision(
            values=cast(dict[str, object], preview.candidate.values),
            credential_refs=preview.candidate.credential_refs,
            schema_version=target.version,
        )
        updated = expected.model_copy(deep=True)
        updated.plugins[candidate.plugin_id].activation.set_configuration(
            candidate.package_digest, revision, activate=target.selected
        )
        outcome: Literal["saved", "published"]
        if target.requires_runtime:
            assert self.runtime is not None
            await self.runtime.apply_candidate_state(
                expected=expected,
                candidate=updated,
                roots={candidate.plugin_id},
                reason="configure",
            )
            outcome = "published"
        else:
            self.store.commit_candidate_state(expected, updated)
            outcome = "saved"
        self.store.append_audit(
            PluginAuditEvent(
                action="configure",
                plugin_id=candidate.plugin_id,
                package_digest=candidate.package_digest,
                request_id=request_id,
            )
        )
        return self._result(target, revision, outcome, updated)

    def _target(
        self, state: PluginStateDocument, plugin_id: str, digest: str | None
    ) -> _ConfigurationTarget:
        record = state.plugins.get(plugin_id)
        if record is None:
            raise KeyError(f"plugin is not installed: {plugin_id}")
        digest = digest if digest is not None else record.activation.selected_digest
        installed = next((item for item in record.installs if item.package_digest == digest), None)
        if installed is None:
            raise ValueError("Plugin configuration target is not installed.")
        revision = record.activation.configuration_for(installed.package_digest)
        if revision is None:
            raise ValueError("Plugin configuration target has no revision.")
        schema = load_configuration_schema(
            self.store.package_path(plugin_id, installed.package_digest), installed.manifest
        )
        return _ConfigurationTarget(record, installed, revision, schema)

    def _preview(
        self, target: _ConfigurationTarget, candidate: PluginConfigurationCandidate
    ) -> PluginConfigurationPreview:
        if target.schema is not None:
            candidate.values = cast(dict[str, JsonValue], target.schema.normalize(candidate.values))
        issues = target.issues(cast(dict[str, object], candidate.values), candidate.credential_refs)
        revision_id = configuration_revision_id(
            values=cast(dict[str, object], candidate.values),
            credential_refs=candidate.credential_refs,
            schema_version=target.version,
        )
        changed = revision_id != target.revision.revision_id
        return PluginConfigurationPreview(
            candidate=candidate,
            valid=not issues,
            issues=issues,
            changed=changed,
            scope="none"
            if not changed
            else "current_host"
            if target.requires_runtime
            else "stored",
        )

    def _result(
        self,
        target: _ConfigurationTarget,
        revision: PluginConfigurationRevision,
        outcome: Literal["unchanged", "saved", "published"],
        state: PluginStateDocument,
    ) -> PluginConfigurationResult:
        record = state.plugins[target.record.plugin_id]
        if self.runtime is not None:
            status = self.runtime.project_status(record, state=state)
        elif not record.activation.desired_enabled:
            status = "disabled"
        else:
            selected = record.selected()
            assert selected is not None
            status = plugin_install_readiness(
                selected,
                record.activation,
                self.store.package_path(record.plugin_id, selected.package_digest),
            )
        return PluginConfigurationResult(
            plugin_id=record.plugin_id,
            package_digest=target.installed.package_digest,
            configuration_revision_id=revision.revision_id,
            outcome=outcome,
            scope="none"
            if outcome == "unchanged"
            else "stored"
            if outcome == "saved"
            else "current_host",
            changed=outcome != "unchanged",
            status=status,
            diagnostics=self.runtime.diagnostics(record.plugin_id)
            if self.runtime is not None
            else [],
        )
