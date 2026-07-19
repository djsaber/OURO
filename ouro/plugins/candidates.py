from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ouro.plugins.builtin import BuiltinPluginFactory
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.models import PluginClass, PluginConfigurationRevision


@dataclass(frozen=True)
class PluginRuntimeCandidate:
    """Resolved code and configuration used to build one plugin generation."""

    plugin_class: PluginClass
    manifest: PluginManifest
    release_id: str
    root: Path
    configuration: PluginConfigurationRevision
    source_kind: str
    public_source: str
    package_digest: str | None = None
    builtin_contract_hash: str | None = None
    factory: BuiltinPluginFactory | None = None

    def __post_init__(self) -> None:
        if self.plugin_class == "user":
            if self.package_digest is None or self.builtin_contract_hash is not None:
                raise ValueError("user runtime candidate requires only a package digest")
            if self.release_id != self.package_digest:
                raise ValueError("user runtime candidate release id must equal package digest")
            if self.factory is not None:
                raise ValueError("user runtime candidate cannot supply a builtin factory")
        elif self.builtin_contract_hash is None or self.package_digest is not None:
            raise ValueError("builtin runtime candidate requires only a builtin contract hash")
        elif self.factory is None:
            raise ValueError("builtin runtime candidate requires an in-process factory")
        elif self.release_id != self.builtin_contract_hash:
            raise ValueError("builtin runtime candidate release id must equal contract hash")

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    @property
    def version(self) -> str:
        return self.manifest.version


__all__ = ["PluginRuntimeCandidate"]
