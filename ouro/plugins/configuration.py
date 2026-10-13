from __future__ import annotations

import re
from collections.abc import Mapping

from ouro.plugins.configuration_schema import ConfigurationIssue, configuration_pointer
from ouro.plugins.manifest import PluginManifest

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def declared_credential_slots(manifest: PluginManifest) -> frozenset[str]:
    return frozenset(
        scope
        for request in manifest.permissions
        if request.id == "credentials.use"
        for scope in request.scopes
    )


def credential_binding_issues(
    manifest: PluginManifest, bindings: Mapping[str, str]
) -> tuple[ConfigurationIssue, ...]:
    declared = declared_credential_slots(manifest)
    issues = []
    for slot, reference in sorted(bindings.items()):
        code = ""
        message = ""
        if slot not in declared:
            code = "undeclared_credential"
            message = "credential slot is not declared by the manifest"
        elif not isinstance(reference, str) or not reference.startswith("env:"):
            code = "credential_reference"
            message = "credential slot must use an env: reference"
        elif _ENVIRONMENT_NAME.fullmatch(reference[4:]) is None:
            code = "credential_reference"
            message = "credential slot has an invalid environment reference"
        if code:
            issues.append(
                ConfigurationIssue(
                    path=configuration_pointer(("credential_refs", slot)),
                    code=code,
                    message=message,
                )
            )
    if manifest.configuration is not None:
        for slot, declaration in sorted(manifest.configuration.credentials.items()):
            if declaration.required and slot not in bindings:
                issues.append(
                    ConfigurationIssue(
                        path=configuration_pointer(("credential_refs", slot)),
                        code="required_credential",
                        message="A required credential slot is not bound.",
                    )
                )
    return tuple(issues)
