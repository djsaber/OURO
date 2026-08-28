from __future__ import annotations

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

from ouro.plugins.manifest import PluginManifest, PluginStatus
from ouro.plugins.models import InstalledPlugin, PluginActivationState
from ouro.plugins.package import resolve_manifest_path

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def plugin_install_readiness(
    installed: InstalledPlugin,
    activation: PluginActivationState,
    package_root: Path,
) -> PluginStatus:
    """Project readiness for one installed digest without activation intent."""

    if configuration_errors(installed, activation, package_root):
        return "needs_configuration"
    return "ready"


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
