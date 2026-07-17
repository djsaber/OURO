from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, OuroModel, utc_now
from ouro.plugins.manifest import PluginManifest, PluginRuntimeKind, PluginStatus

PluginSourceKind = Literal["local", "git", "archive"]
PluginScope = Literal["user", "workspace"]
DiagnosticSeverity = Literal["info", "warning", "error"]


class PluginProvenance(OuroModel):
    source_kind: PluginSourceKind
    source_reference: str
    public_source: str
    resolved_reference: str | None = None
    package_digest: str
    installed_at: datetime = Field(default_factory=utc_now)
    scope: PluginScope = "user"
    installer_version: str = "0.1.0"


class InstalledPlugin(OuroModel):
    plugin_id: str
    version: str
    package_digest: str
    manifest: PluginManifest
    provenance: PluginProvenance
    installed_at: datetime = Field(default_factory=utc_now)


class CapabilityGrant(OuroModel):
    plugin_id: str
    scope: PluginScope
    permission_fingerprint: str
    contribution_fingerprint: str
    runtime_kind: PluginRuntimeKind
    granted_at: datetime = Field(default_factory=utc_now)


class CodeTrustGrant(OuroModel):
    plugin_id: str
    scope: PluginScope
    package_digest: str
    runtime_kind: PluginRuntimeKind
    granted_at: datetime = Field(default_factory=utc_now)


class PluginConfigurationState(OuroModel):
    values: dict[str, object] = Field(default_factory=dict)
    credential_refs: dict[str, str] = Field(default_factory=dict)
    schema_version: int = 1


class PluginActivationState(OuroModel):
    desired_enabled: bool = False
    scope: PluginScope = "user"
    selected_digest: str | None = None
    capability_grants: list[CapabilityGrant] = Field(default_factory=list)
    code_trust_grants: list[CodeTrustGrant] = Field(default_factory=list)
    configuration: PluginConfigurationState = Field(default_factory=PluginConfigurationState)
    configurations: dict[str, PluginConfigurationState] = Field(default_factory=dict)

    def configuration_for(self, package_digest: str) -> PluginConfigurationState:
        return self.configurations.get(package_digest, self.configuration)


class PluginRecord(OuroModel):
    plugin_id: str
    installs: list[InstalledPlugin] = Field(default_factory=list)
    activation: PluginActivationState = Field(default_factory=PluginActivationState)

    def selected(self) -> InstalledPlugin | None:
        digest = self.activation.selected_digest
        if digest is None:
            return None
        return next((item for item in self.installs if item.package_digest == digest), None)


class PluginStateDocument(OuroModel):
    schema_version: Literal[1] = 1
    plugins: dict[str, PluginRecord] = Field(default_factory=dict)


class PluginDiagnostic(OuroModel):
    code: str
    phase: str
    severity: DiagnosticSeverity = "error"
    message: str
    retryable: bool = False
    suggested_action: str | None = None
    plugin_id: str | None = None
    version: str | None = None
    package_digest: str | None = None
    generation_id: str | None = None
    contribution_id: str | None = None
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class PluginGenerationRecord(OuroModel):
    generation_id: str
    plugin_id: str
    version: str
    package_digest: str
    runtime_kind: PluginRuntimeKind
    contract_hashes: dict[str, str] = Field(default_factory=dict)
    status: PluginStatus = "starting"
    created_at: datetime = Field(default_factory=utc_now)


class PluginAuditEvent(OuroModel):
    action: str
    plugin_id: str | None = None
    package_digest: str | None = None
    result: Literal["succeeded", "failed"] = "succeeded"
    detail: str = ""
    request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Metadata = MetadataField
