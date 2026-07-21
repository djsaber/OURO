from __future__ import annotations

from typing import Literal

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.session_controls import AttributedSessionInput
from ouro.plugins.manifest import PluginRuntimeKind, PluginStatus
from ouro.plugins.models import PluginClass, PluginDiagnostic


class PluginProvenanceView(OuroModel):
    source_kind: str
    public_source: str
    resolved_reference: str | None = None


class PluginInstallView(OuroModel):
    plugin_id: str
    name: str
    version: str
    package_digest: str
    runtime_kind: PluginRuntimeKind
    plugin_class: PluginClass
    selected: bool = False
    provenance: PluginProvenanceView


class PluginSummary(OuroModel):
    plugin_id: str
    name: str
    version: str
    package_digest: str | None = None
    builtin_contract_hash: str | None = None
    status: PluginStatus
    runtime_kind: PluginRuntimeKind
    desired_enabled: bool
    plugin_class: PluginClass
    default_enabled: bool | None = None


class PluginDetail(PluginSummary):
    description: str = ""
    namespace: str
    installs: list[PluginInstallView] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    permissions: list[dict[str, object]] = Field(default_factory=list)
    contributions: dict[str, list[str]] = Field(default_factory=dict)
    dependencies: list[dict[str, str | None]] = Field(default_factory=list)
    active_generations: list[dict[str, object]] = Field(default_factory=list)
    diagnostics: list[PluginDiagnostic] = Field(default_factory=list)


class PluginValidationView(OuroModel):
    valid: bool
    plugin_id: str | None = None
    name: str | None = None
    version: str | None = None
    package_digest: str | None = None
    runtime_kind: PluginRuntimeKind | None = None
    file_count: int = 0
    total_bytes: int = 0
    diagnostics: list[PluginDiagnostic] = Field(default_factory=list)


class PluginScaffoldView(OuroModel):
    root: str
    plugin_id: str
    name: str
    version: str
    namespace: str
    runtime_kind: PluginRuntimeKind
    package_digest: str
    files: list[str] = Field(default_factory=list)


class PluginActionResult(OuroModel):
    action: str
    plugin_id: str
    status: PluginStatus
    changed: bool = True
    package_digest: str | None = None
    diagnostics: list[PluginDiagnostic] = Field(default_factory=list)


class PluginCommandView(OuroModel):
    command_id: str
    plugin_id: str
    name: str
    description: str = ""
    usage: str = ""


class PluginCommandExecutionView(OuroModel):
    command_id: str
    output: str = ""
    data: dict[str, object] = Field(default_factory=dict)
    session_inputs: list[AttributedSessionInput] = Field(default_factory=list)


class CompatibilityItemView(OuroModel):
    kind: str
    identity: str
    outcome: Literal["supported", "converted", "ignored", "incompatible"]
    reason: str = ""
    ouro_target: str | None = None


class CompatibilityReportView(OuroModel):
    source_format: str
    source_version: str | None = None
    detected_resources: list[str] = Field(default_factory=list)
    items: list[CompatibilityItemView] = Field(default_factory=list)
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    lost_semantics: list[str] = Field(default_factory=list)
    security_differences: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)
    suggested_target: str = "native"
