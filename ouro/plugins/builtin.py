from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from ouro.plugins.dependencies import BuiltinPluginContract, resolve_plugin_dependencies
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.models import PluginStateDocument
from ouro.plugins.paths import portable_relative_path

if TYPE_CHECKING:
    from ouro.sdk.plugins.v5 import PluginContext


BuiltinPluginFactory = Callable[["PluginContext"], object]


@dataclass(frozen=True)
class BuiltinPluginDefinition:
    """One product-owned plugin release supplied by the composition root."""

    manifest: PluginManifest
    factory: BuiltinPluginFactory
    root: Path
    default_enabled: bool = True
    runtime_resources: tuple[str, ...] = ()
    _contract_hash: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        object.__setattr__(self, "root", root)
        if not root.is_dir():
            raise ValueError(f"builtin plugin root is not a directory: {root}")
        if self.manifest.runtime.kind != "python-inprocess":
            raise ValueError("builtin plugins currently require python-inprocess runtime")
        if self.manifest.configuration is not None:
            raise ValueError("builtin plugin configuration is not supported yet")
        if self.manifest.contributes.skills or self.manifest.contributes.mcp_servers:
            raise ValueError("builtin package resource contributions are not supported yet")
        if not callable(self.factory):
            raise TypeError("builtin plugin factory must be callable")
        object.__setattr__(
            self,
            "_contract_hash",
            _builtin_contract_hash(self.manifest, root, self.runtime_resources),
        )

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def contract_hash(self) -> str:
        return self._contract_hash

    def dependency_contract(self) -> BuiltinPluginContract:
        return BuiltinPluginContract(
            plugin_id=self.plugin_id,
            version=self.version,
            contract_hash=self.contract_hash,
            dependencies=tuple(
                (dependency.plugin_id, dependency.version)
                for dependency in self.manifest.dependencies.plugins
            ),
            default_enabled=self.default_enabled,
        )


class BuiltinPluginRegistry:
    """Immutable application inventory of explicitly assembled builtin plugins."""

    def __init__(self, definitions: Iterable[BuiltinPluginDefinition] = ()) -> None:
        indexed: dict[str, BuiltinPluginDefinition] = {}
        exposed_tools: dict[str, str] = {}
        for definition in definitions:
            if definition.plugin_id in indexed:
                raise ValueError(f"duplicate builtin plugin id: {definition.plugin_id}")
            indexed[definition.plugin_id] = definition
            for tool in definition.manifest.contributes.tools:
                previous = exposed_tools.setdefault(tool.exposed_name, definition.plugin_id)
                if previous != definition.plugin_id:
                    raise ValueError(
                        "duplicate builtin tool exposed name: "
                        f"{tool.exposed_name} ({previous}, {definition.plugin_id})"
                    )
        for definition in indexed.values():
            for dependency in definition.manifest.dependencies.plugins:
                if dependency.plugin_id not in indexed:
                    raise ValueError(
                        "builtin plugin dependency is not supplied by the application: "
                        f"{definition.plugin_id} -> {dependency.plugin_id}"
                    )
        self._definitions: Mapping[str, BuiltinPluginDefinition] = MappingProxyType(indexed)
        self._exposed_tools = frozenset(exposed_tools)
        resolve_plugin_dependencies(
            PluginStateDocument(),
            roots=set(indexed),
            builtin_contracts=self.contracts,
        )

    def get(self, plugin_id: str) -> BuiltinPluginDefinition | None:
        return self._definitions.get(plugin_id)

    def require(self, plugin_id: str) -> BuiltinPluginDefinition:
        try:
            return self._definitions[plugin_id]
        except KeyError as exc:
            raise KeyError(f"builtin plugin is not available: {plugin_id}") from exc

    def list(self) -> tuple[BuiltinPluginDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    @property
    def plugin_ids(self) -> frozenset[str]:
        return frozenset(self._definitions)

    @property
    def contracts(self) -> dict[str, BuiltinPluginContract]:
        return {
            definition.plugin_id: definition.dependency_contract()
            for definition in self._definitions.values()
        }

    def assert_user_manifest_allowed(self, manifest: PluginManifest) -> None:
        if manifest.plugin_id in self._definitions:
            raise ValueError(f"user plugin cannot replace builtin identity: {manifest.plugin_id}")
        conflicts = sorted(
            tool.exposed_name
            for tool in manifest.contributes.tools
            if tool.exposed_name in self._exposed_tools
        )
        if conflicts:
            raise ValueError(
                "user plugin cannot replace builtin tool names: " + ", ".join(conflicts)
            )


EMPTY_BUILTIN_PLUGIN_REGISTRY = BuiltinPluginRegistry()


def _builtin_contract_hash(
    manifest: PluginManifest, root: Path, runtime_resources: tuple[str, ...]
) -> str:
    """Bind product-owned Python code and explicitly declared runtime data."""

    inputs = {path for path in root.rglob("*.py") if path.is_file()}
    inputs.update(root / portable_relative_path(resource) for resource in runtime_resources)
    implementation: list[dict[str, str]] = []
    for path in sorted(
        inputs,
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        implementation.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    payload = {
        "schema_version": 2,
        "manifest": manifest.model_dump(mode="json", exclude_none=True),
        "implementation": implementation,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BuiltinPluginDefinition",
    "BuiltinPluginFactory",
    "BuiltinPluginRegistry",
    "EMPTY_BUILTIN_PLUGIN_REGISTRY",
]
