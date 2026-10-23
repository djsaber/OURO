from __future__ import annotations

from pathlib import Path

from ouro.plugins.configuration.service import credential_binding_issues
from ouro.plugins.installation.package import load_configuration_schema
from ouro.plugins.installation.paths import PluginPackageError
from ouro.plugins.manifest import PluginStatus
from ouro.plugins.models import InstalledPlugin, PluginActivationState


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
    specification = installed.manifest.configuration
    configuration = activation.configuration_for(installed.package_digest)
    if configuration is None:
        return ["plugin has no configuration revision for the selected package"]
    issues = credential_binding_issues(installed.manifest, configuration.credential_refs)
    errors = [issue.message for issue in issues]
    version = specification.version if specification is not None else None
    if configuration.schema_version != version:
        return [*errors, "plugin configuration schema version does not match the selected package"]
    if specification is None or specification.schema_path is None:
        if configuration.values:
            return [
                *errors,
                "plugin without a configuration schema requires an empty configuration",
            ]
        return errors
    try:
        schema = load_configuration_schema(package_root, installed.manifest)
        assert schema is not None
        return [*errors, *(issue.message for issue in schema.validate(configuration.values_dict()))]
    except PluginPackageError, ValueError:
        return [*errors, "invalid plugin configuration schema"]
