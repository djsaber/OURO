from __future__ import annotations

from typing import Literal, cast

from pydantic import ConfigDict, Field, JsonValue, field_validator

from ouro.core.base import OuroModel
from ouro.plugins.configuration.schema import ConfigurationIssue as ConfigurationIssue
from ouro.plugins.configuration.values import copy_configuration_values
from ouro.plugins.credentials import InlinePluginCredential as InlinePluginCredential
from ouro.plugins.credentials import PluginCredentialBinding
from ouro.plugins.credentials import parse_credential_bindings as parse_credential_bindings
from ouro.plugins.manifest import PluginStatus
from ouro.plugins.models import PluginDiagnostic


class PluginCredentialSlotView(OuroModel):
    slot: str
    title: str
    description: str = ""
    required: bool = False
    bound: bool = False
    availability: Literal["unknown"] = "unknown"


class PluginConfigurationCandidate(OuroModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)

    plugin_id: str
    package_digest: str
    expected_configuration_revision_id: str
    values: dict[str, JsonValue]
    credential_refs: dict[str, PluginCredentialBinding]

    @field_validator("values", mode="before")
    @classmethod
    def _copy_values(cls, value: object) -> dict[str, object]:
        return copy_configuration_values(value)


class PluginConfigurationView(OuroModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    plugin_id: str
    name: str
    package_digest: str
    selected: bool
    desired_enabled: bool
    configuration_supported: bool
    configuration_version: int | None
    configuration_revision_id: str
    schema_document: dict[str, JsonValue] | None
    values: dict[str, JsonValue]
    credential_slots: tuple[PluginCredentialSlotView, ...]
    credential_refs: dict[str, PluginCredentialBinding]
    issues: tuple[ConfigurationIssue, ...]

    def candidate(
        self,
        *,
        values: dict[str, object] | None = None,
        credential_refs: dict[str, PluginCredentialBinding] | None = None,
    ) -> PluginConfigurationCandidate:
        """Create a detached full draft retaining this view's target and edit baseline."""

        return PluginConfigurationCandidate(
            plugin_id=self.plugin_id,
            package_digest=self.package_digest,
            expected_configuration_revision_id=self.configuration_revision_id,
            values=cast(dict[str, JsonValue], self.values if values is None else values),
            credential_refs=dict(
                self.credential_refs if credential_refs is None else credential_refs
            ),
        )


class PluginConfigurationPreview(OuroModel):
    candidate: PluginConfigurationCandidate
    valid: bool
    issues: tuple[ConfigurationIssue, ...]
    changed: bool
    scope: Literal["none", "stored", "current_host"]


class PluginConfigurationResult(OuroModel):
    action: Literal["configure"] = "configure"
    plugin_id: str
    package_digest: str
    configuration_revision_id: str
    outcome: Literal["unchanged", "saved", "published"]
    scope: Literal["none", "stored", "current_host"]
    changed: bool
    status: PluginStatus
    diagnostics: list[PluginDiagnostic] = Field(default_factory=list)
