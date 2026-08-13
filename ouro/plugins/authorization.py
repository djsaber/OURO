from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from ouro.plugins.manifest import PluginManifest, PluginStatus
from ouro.plugins.models import (
    CapabilityGrant,
    CodeTrustGrant,
    InstalledPlugin,
    PluginActivationState,
    PluginTrustStatus,
)
from ouro.plugins.package import resolve_manifest_path

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class PluginTrustAssessment:
    """Host-derived trust requirements for one selected user-plugin digest."""

    capability_authorization: PluginTrustStatus
    code_trust: PluginTrustStatus


def permission_fingerprint(manifest: PluginManifest) -> str:
    values = sorted(
        (request.model_dump(mode="json", exclude_none=True) for request in manifest.permissions),
        key=lambda item: str(item["id"]),
    )
    return _fingerprint(values)


def contribution_fingerprint(manifest: PluginManifest) -> str:
    values = {
        kind: sorted(
            (item.model_dump(mode="json", exclude_none=True) for item in items),
            key=lambda item: str(item["id"]),
        )
        for kind, items in manifest.contributes.by_kind().items()
        if items
    }
    return _fingerprint(values)


def permission_request_fingerprint(manifest: PluginManifest) -> str:
    return _fingerprint(
        {
            "permissions": permission_fingerprint(manifest),
            "contributions": contribution_fingerprint(manifest),
            "runtime": manifest.runtime.kind,
        }
    )


def create_capability_grant(
    installed: InstalledPlugin,
) -> CapabilityGrant:
    manifest = installed.manifest
    return CapabilityGrant(
        plugin_id=installed.plugin_id,
        permission_fingerprint=permission_fingerprint(manifest),
        contribution_fingerprint=contribution_fingerprint(manifest),
        runtime_kind=manifest.runtime.kind,
    )


def create_code_trust_grant(
    installed: InstalledPlugin,
) -> CodeTrustGrant:
    return CodeTrustGrant(
        plugin_id=installed.plugin_id,
        package_digest=installed.package_digest,
        runtime_kind=installed.manifest.runtime.kind,
    )


def capability_grant_is_valid(
    installed: InstalledPlugin,
    activation: PluginActivationState,
) -> bool:
    if not requires_capability_authorization(installed.manifest):
        return True
    return any(
        grant.plugin_id == installed.plugin_id
        and grant.permission_fingerprint == permission_fingerprint(installed.manifest)
        and grant.contribution_fingerprint == contribution_fingerprint(installed.manifest)
        and grant.runtime_kind == installed.manifest.runtime.kind
        for grant in activation.capability_grants
    )


def code_trust_is_valid(
    installed: InstalledPlugin,
    activation: PluginActivationState,
) -> bool:
    if not requires_code_trust(installed.manifest):
        return True
    for grant in activation.code_trust_grants:
        if (
            grant.plugin_id == installed.plugin_id
            and grant.runtime_kind == installed.manifest.runtime.kind
            and grant.package_digest == installed.package_digest
        ):
            return True
    return False


def plugin_install_readiness(
    installed: InstalledPlugin,
    activation: PluginActivationState,
    package_root: Path,
) -> PluginStatus:
    """Project readiness for one installed digest without activation intent."""

    if configuration_errors(installed, activation, package_root):
        return "needs_configuration"
    trust = assess_plugin_trust(installed, activation)
    if trust.code_trust == "missing":
        return "needs_code_trust"
    if trust.capability_authorization == "missing":
        return "needs_authorization"
    return "ready"


def assess_plugin_trust(
    installed: InstalledPlugin,
    activation: PluginActivationState,
) -> PluginTrustAssessment:
    """Evaluate grants against requirements derived exclusively by the Host."""

    return PluginTrustAssessment(
        capability_authorization=_trust_status(
            required=requires_capability_authorization(installed.manifest),
            granted=capability_grant_is_valid(installed, activation),
        ),
        code_trust=_trust_status(
            required=requires_code_trust(installed.manifest),
            granted=code_trust_is_valid(installed, activation),
        ),
    )


def requires_capability_authorization(manifest: PluginManifest) -> bool:
    """Require consent for every declared permission or Host contribution."""

    return bool(manifest.permissions or manifest.contributes.capabilities())


def requires_code_trust(manifest: PluginManifest) -> bool:
    return manifest.runtime.kind != "declarative" or any(
        request.id == "process.spawn" for request in manifest.permissions
    )


def configuration_errors(
    installed: InstalledPlugin,
    activation: PluginActivationState,
    package_root: Path,
) -> list[str]:
    binding_errors = _credential_binding_errors(installed, activation)
    if binding_errors:
        return binding_errors
    specification = installed.manifest.configuration
    configuration = activation.configuration_for(installed.package_digest)
    if configuration is None:
        return ["plugin has no configuration revision for the selected package"]
    if specification is None:
        if configuration.schema_version is not None or configuration.values:
            return ["plugin without a configuration schema requires an empty configuration"]
        return []
    schema_path = resolve_manifest_path(
        package_root,
        specification.schema_path,
        allow_directory=False,
    )
    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [f"invalid plugin configuration schema: {type(exc).__name__}"]
    if configuration.schema_version != specification.version:
        return ["plugin configuration schema version does not match the selected package"]
    return [error.message for error in validator.iter_errors(configuration.values)]


def declared_credential_slots(manifest: PluginManifest) -> frozenset[str]:
    return frozenset(
        scope
        for request in manifest.permissions
        if request.id == "credentials.use"
        for scope in request.scopes
    )


def _credential_binding_errors(
    installed: InstalledPlugin,
    activation: PluginActivationState,
) -> list[str]:
    configuration = activation.configuration_for(installed.package_digest)
    if configuration is None:
        return ["plugin has no configuration revision for the selected package"]
    declared = declared_credential_slots(installed.manifest)
    errors: list[str] = []
    for slot, reference in configuration.credential_refs.items():
        if slot not in declared:
            errors.append(f"credential slot is not declared by the manifest: {slot}")
            continue
        if not reference.startswith("env:"):
            errors.append(f"credential slot must use an env: reference: {slot}")
            continue
        name = reference.removeprefix("env:")
        if _ENVIRONMENT_NAME.fullmatch(name) is None:
            errors.append(f"credential slot has an invalid environment reference: {slot}")
    return errors


def _trust_status(*, required: bool, granted: bool) -> PluginTrustStatus:
    if not required:
        return "not_required"
    return "granted" if granted else "missing"


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
