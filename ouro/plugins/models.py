from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from ouro import __version__
from ouro.core.base import Metadata, MetadataField, OuroModel, utc_now
from ouro.plugins.configuration_values import (
    configuration_dict,
    copy_configuration_values,
    freeze_configuration,
)
from ouro.plugins.credentials import (
    PluginCredentialBinding,
    credential_bindings_payload,
    parse_credential_bindings,
)
from ouro.plugins.manifest import PluginManifest, PluginRuntimeKind, PluginStatus

PluginSourceKind = Literal["local", "git", "archive"]
PluginClass = Literal["builtin", "user"]
DiagnosticSeverity = Literal["info", "warning", "error"]


class PluginProvenance(OuroModel):
    source_kind: PluginSourceKind
    source_reference: str
    public_source: str
    resolved_reference: str | None = None
    package_digest: str
    installed_at: datetime = Field(default_factory=utc_now)
    installer_version: str = __version__


class InstalledPlugin(OuroModel):
    plugin_id: str
    version: str
    package_digest: str
    manifest: PluginManifest
    provenance: PluginProvenance
    installed_at: datetime = Field(default_factory=utc_now)


class PluginConfigurationRevision(OuroModel):
    """Immutable configuration with explicit env references or persisted inline keys."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    revision_id: str
    values: Mapping[str, object] = Field(default_factory=dict)
    credential_refs: Mapping[str, PluginCredentialBinding] = Field(default_factory=dict)
    schema_version: int | None = Field(default=None, ge=1, strict=True)

    @field_validator("values", mode="before")
    @classmethod
    def _validate_values(cls, value: object) -> dict[str, object]:
        return copy_configuration_values(value)

    @field_serializer("values")
    def _serialize_values(self, values: Mapping[str, object]) -> dict[str, object]:
        return configuration_dict(values)

    @field_serializer("credential_refs")
    def _serialize_references(
        self, values: Mapping[str, PluginCredentialBinding]
    ) -> dict[str, PluginCredentialBinding]:
        return dict(values)

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Self:
        return self

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        # This immutable value can be shared; updates must still verify its digest.
        if not update:
            return self
        return type(self).model_validate({**self.model_dump(), **update})

    @model_validator(mode="after")
    def _validate_revision_id(self) -> PluginConfigurationRevision:
        expected = configuration_revision_id(
            values=self.values,
            credential_refs=self.credential_refs,
            schema_version=self.schema_version,
        )
        if self.revision_id != expected:
            raise ValueError("plugin configuration revision id does not match its content")
        object.__setattr__(self, "values", freeze_configuration(self.values))
        object.__setattr__(self, "credential_refs", MappingProxyType(dict(self.credential_refs)))
        return self

    def values_dict(self) -> dict[str, object]:
        return configuration_dict(self.values)


class PluginActivationState(OuroModel):
    desired_enabled: bool = False
    selected_digest: str | None = None
    configuration_revisions: dict[str, PluginConfigurationRevision] = Field(default_factory=dict)
    configuration_revision_by_digest: dict[str, str] = Field(default_factory=dict)
    active_configuration_revision_id: str | None = None

    def configuration_for(self, package_digest: str) -> PluginConfigurationRevision | None:
        revision_id = self.configuration_revision_by_digest.get(package_digest)
        if revision_id is None:
            return None
        return self.configuration_revisions.get(revision_id)

    def set_configuration(
        self,
        package_digest: str,
        revision: PluginConfigurationRevision,
        *,
        activate: bool,
    ) -> None:
        existing = self.configuration_revisions.get(revision.revision_id)
        if existing is not None and existing != revision:
            raise ValueError("plugin configuration revision id collision")
        self.configuration_revisions[revision.revision_id] = revision
        self.configuration_revision_by_digest[package_digest] = revision.revision_id
        if activate:
            self.active_configuration_revision_id = revision.revision_id

    def remove_configuration_for(self, package_digest: str) -> None:
        # Revisions belong to the plugin, not a selected package. A Run may
        # still reference a revision after that package's configuration changes.
        self.configuration_revision_by_digest.pop(package_digest, None)


class PluginRecord(OuroModel):
    plugin_id: str
    installs: list[InstalledPlugin] = Field(default_factory=list)
    activation: PluginActivationState = Field(default_factory=PluginActivationState)

    def selected(self) -> InstalledPlugin | None:
        digest = self.activation.selected_digest
        if digest is None:
            return None
        return next((item for item in self.installs if item.package_digest == digest), None)


class BuiltinPluginActivationOverride(OuroModel):
    desired_enabled: bool


class PluginStateDocument(OuroModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    schema_version: Literal[4] = 4
    plugins: dict[str, PluginRecord] = Field(default_factory=dict)
    builtin_activation_overrides: dict[str, BuiltinPluginActivationOverride] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _validate_records(self) -> PluginStateDocument:
        for plugin_id, record in self.plugins.items():
            if record.plugin_id != plugin_id:
                raise ValueError("plugin state key does not match record identity")
            if not record.installs:
                raise ValueError(f"plugin state has no installed packages: {plugin_id}")
            digests = [installed.package_digest for installed in record.installs]
            if len(digests) != len(set(digests)):
                raise ValueError(f"plugin state has duplicate package digests: {plugin_id}")
            for installed in record.installs:
                if installed.plugin_id != plugin_id or installed.manifest.plugin_id != plugin_id:
                    raise ValueError(f"plugin install identity is inconsistent: {plugin_id}")
                if installed.provenance.package_digest != installed.package_digest:
                    raise ValueError(f"plugin provenance digest is inconsistent: {plugin_id}")

            activation = record.activation
            if activation.selected_digest not in set(digests):
                raise ValueError(f"selected plugin digest is not installed: {plugin_id}")
            if set(activation.configuration_revision_by_digest) != set(digests):
                raise ValueError(
                    f"plugin configuration revisions do not match installed digests: {plugin_id}"
                )
            for revision_id, revision in activation.configuration_revisions.items():
                if revision_id != revision.revision_id:
                    raise ValueError(f"plugin configuration revision key is invalid: {plugin_id}")
            for revision_id in activation.configuration_revision_by_digest.values():
                if revision_id not in activation.configuration_revisions:
                    raise ValueError(f"plugin configuration revision is missing: {plugin_id}")
            selected_revision = activation.configuration_revision_by_digest[
                activation.selected_digest
            ]
            if activation.active_configuration_revision_id != selected_revision:
                raise ValueError(f"active plugin configuration revision is invalid: {plugin_id}")
        return self


class PluginDiagnostic(OuroModel):
    code: str
    phase: str
    severity: DiagnosticSeverity = "error"
    message: str
    retryable: bool = False
    suggested_action: str | None = None
    plugin_id: str | None = None
    version: str | None = None
    plugin_class: PluginClass | None = None
    release_id: str | None = None
    package_digest: str | None = None
    builtin_contract_hash: str | None = None
    generation_id: str | None = None
    contribution_id: str | None = None
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class PluginGenerationRecord(OuroModel):
    generation_id: str
    plugin_id: str
    version: str
    plugin_class: PluginClass = "user"
    release_id: str = ""
    package_digest: str | None = None
    builtin_contract_hash: str | None = None
    runtime_kind: PluginRuntimeKind
    configuration_revision_id: str
    dependency_lock_fingerprint: str
    plugin_api_version: str
    worker_protocol_version: str | None = None
    contract_hashes: dict[str, str] = Field(default_factory=dict)
    execution_contract_hash: str | None = None
    status: PluginStatus = "starting"
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_execution_contract(self) -> PluginGenerationRecord:
        if self.plugin_class == "user":
            if self.package_digest is None or self.builtin_contract_hash is not None:
                raise ValueError("user plugin generation requires only a package digest")
            if not self.release_id:
                self.release_id = self.package_digest
            elif self.release_id != self.package_digest:
                raise ValueError("user plugin generation release id must equal package digest")
        elif self.builtin_contract_hash is None or self.package_digest is not None:
            raise ValueError("builtin plugin generation requires only a builtin contract hash")
        elif not self.release_id:
            raise ValueError("builtin plugin generation requires a release id")
        elif self.release_id != self.builtin_contract_hash:
            raise ValueError("builtin plugin generation release id must equal contract hash")
        if self.status in {"active", "degraded"} and self.execution_contract_hash is None:
            raise ValueError("active plugin generation requires an execution contract")
        for field_name, value in (
            ("configuration_revision_id", self.configuration_revision_id),
            ("dependency_lock_fingerprint", self.dependency_lock_fingerprint),
        ):
            if not _is_sha256_digest(value):
                raise ValueError(f"plugin generation {field_name} must be a SHA-256 digest")
        if self.execution_contract_hash is not None and not _is_sha256_digest(
            self.execution_contract_hash
        ):
            raise ValueError("plugin generation execution_contract_hash must be a SHA-256 digest")
        return self


class PluginAuditEvent(OuroModel):
    action: str
    plugin_id: str | None = None
    package_digest: str | None = None
    result: Literal["succeeded", "failed"] = "succeeded"
    detail: str = ""
    request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    metadata: Metadata = MetadataField


def create_configuration_revision(
    *,
    values: dict[str, object] | None = None,
    credential_refs: dict[str, PluginCredentialBinding] | None = None,
    schema_version: int | None = None,
) -> PluginConfigurationRevision:
    normalized_values = copy_configuration_values(values if values is not None else {})
    normalized_references = parse_credential_bindings(
        credential_refs if credential_refs is not None else {}
    )
    return PluginConfigurationRevision(
        revision_id=configuration_revision_id(
            values=normalized_values,
            credential_refs=normalized_references,
            schema_version=schema_version,
        ),
        values=normalized_values,
        credential_refs=normalized_references,
        schema_version=schema_version,
    )


def configuration_revision_id(
    *,
    values: Mapping[str, object],
    credential_refs: Mapping[str, PluginCredentialBinding],
    schema_version: int | None,
) -> str:
    encoded = json.dumps(
        {
            "schema_version": schema_version,
            "values": configuration_dict(values),
            "credential_refs": credential_bindings_payload(credential_refs),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
