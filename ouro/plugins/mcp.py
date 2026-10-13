from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ouro.mcp import MCPManager, MCPServerConfig
from ouro.plugins.configuration import declared_credential_slots
from ouro.plugins.manifest import PathContributionSpec, PluginManifest
from ouro.plugins.package import PluginPackageError, resolve_manifest_path
from ouro.tools import Tool, ToolRegistry


class PluginMCPResource:
    """Plugin-owned MCP connections projected through one immutable generation."""

    def __init__(
        self,
        manifest: PluginManifest,
        package_root: Path,
        *,
        resolve_credential: Callable[[str], str | None] | None = None,
        credential_bindings: Mapping[str, str] | None = None,
    ) -> None:
        self.manifest = manifest
        self.package_root = package_root
        self.registry = ToolRegistry()
        self._resolve_reference = resolve_credential
        self._credential_bindings = dict(credential_bindings or {})
        self.manager = MCPManager(
            registry=self.registry,
            resolve_credential=self._resolve_plugin_credential,
        )
        self._configured: list[tuple[PathContributionSpec, MCPServerConfig]] = []
        self.degraded_reasons: list[str] = []
        self.active_server_ids: list[str] = []

    async def start(self) -> tuple[Tool, ...]:
        for declaration in self.manifest.contributes.mcp_servers:
            config = load_plugin_mcp_config(
                self.manifest,
                declaration,
                package_root=self.package_root,
            )
            self.manager.configure(config)
            self._configured.append((declaration, config))

        for declaration, config in self._configured:
            record = await self.manager.start(config.server_id)
            if record.status != "connected":
                detail = record.error or record.status
                if not declaration.optional:
                    raise RuntimeError(
                        f"plugin MCP server failed to start: {declaration.id}: {detail}"
                    )
                self.degraded_reasons.append(f"mcp_servers/{declaration.id}: {detail}")
                continue
            self.active_server_ids.append(declaration.id)
        return tuple(self.registry.get(name) for name in self.registry.names())

    async def close(self) -> None:
        await self.manager.close()

    def _resolve_plugin_credential(self, reference: str) -> str | None:
        if not reference.startswith("slot:"):
            raise PermissionError("plugin MCP credentials must reference a configured slot")
        slot = reference.removeprefix("slot:")
        try:
            bound_reference = self._credential_bindings[slot]
        except KeyError as exc:
            raise PermissionError(f"plugin credential slot is not bound: {slot}") from exc
        if self._resolve_reference is None:
            raise RuntimeError("plugin credential resolver is unavailable")
        return self._resolve_reference(bound_reference)


def validate_plugin_mcp_descriptors(manifest: PluginManifest, package_root: Path) -> None:
    for declaration in manifest.contributes.mcp_servers:
        load_plugin_mcp_config(manifest, declaration, package_root=package_root)


def load_plugin_mcp_config(
    manifest: PluginManifest,
    declaration: PathContributionSpec,
    *,
    package_root: Path,
) -> MCPServerConfig:
    path = resolve_manifest_path(package_root, declaration.path, allow_directory=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginPackageError(
            f"invalid MCP descriptor {declaration.path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PluginPackageError(f"MCP descriptor must be an object: {declaration.path}")
    raw = _select_server(payload, declaration.id)
    transport = str(raw.get("transport") or raw.get("type") or "stdio")
    if transport == "streamable-http":
        transport = "http"
    server_id = _server_id(manifest.namespace, declaration.id)
    values: dict[str, Any] = {
        **raw,
        "server_id": server_id,
        "name": str(raw.get("name") or f"{manifest.name}: {declaration.id}"),
        "transport": transport,
        "enabled": True,
        "auto_start": True,
        "permission_mode": "approval",
        "metadata": {
            **_mapping(raw.get("metadata"), field="metadata"),
            "plugin_id": manifest.plugin_id,
            "plugin_contribution_id": declaration.id,
        },
    }
    for community_field in ("type",):
        values.pop(community_field, None)
    if transport == "stdio":
        _require_permission(manifest, "process.spawn", declaration.id)
        values["command"] = _resolve_executable(
            package_root,
            str(raw.get("command") or ""),
        )
        values["args"] = [
            _resolve_argument(package_root, str(argument))
            for argument in _string_list(raw.get("args"), field="args")
        ]
        values["cwd"] = _resolve_cwd(package_root, raw.get("cwd"))
    else:
        _require_permission(manifest, "network.outbound", declaration.id)
    credential_refs = _mapping(raw.get("credential_refs"), field="credential_refs")
    if any(not str(reference).startswith("slot:") for reference in credential_refs.values()):
        raise PluginPackageError("plugin MCP credentials must use slot: references")
    declared_slots = declared_credential_slots(manifest)
    for reference in credential_refs.values():
        slot = str(reference).removeprefix("slot:")
        if slot not in declared_slots:
            raise PluginPackageError(f"plugin MCP credential slot is not declared: {slot}")
    values["credential_refs"] = {str(key): str(value) for key, value in credential_refs.items()}
    allowed = set(MCPServerConfig.model_fields)
    return MCPServerConfig.model_validate(
        {key: value for key, value in values.items() if key in allowed}
    )


def _select_server(payload: Mapping[str, object], identity: str) -> dict[str, object]:
    for key in ("mcpServers", "servers"):
        collection = payload.get(key)
        if isinstance(collection, dict):
            selected = collection.get(identity)
            if selected is None and len(collection) == 1:
                selected = next(iter(collection.values()))
            if not isinstance(selected, dict):
                raise PluginPackageError(f"MCP descriptor does not contain server {identity!r}")
            return {str(name): value for name, value in selected.items()}
    return {str(name): value for name, value in payload.items()}


def _server_id(namespace: str, local_id: str) -> str:
    base = f"{namespace}_{local_id}".replace("-", "_")
    if len(base) <= 32:
        return base
    digest = hashlib.sha256(base.encode()).hexdigest()[:8]
    return f"{base[:23]}_{digest}"


def _resolve_executable(package_root: Path, command: str) -> str:
    if command == "{python}":
        return sys.executable
    if not command.startswith("./"):
        raise PluginPackageError("plugin MCP stdio command must be '{python}' or package-relative")
    return str(resolve_manifest_path(package_root, command, allow_directory=False))


def _resolve_argument(package_root: Path, argument: str) -> str:
    if argument.startswith("./"):
        return str(resolve_manifest_path(package_root, argument, allow_directory=True))
    if argument.startswith(("../", "/")) or "\\" in argument:
        raise PluginPackageError(
            f"plugin MCP argument path must be normalized and package-relative: {argument}"
        )
    return argument


def _resolve_cwd(package_root: Path, value: object) -> str:
    if value is None:
        return str(package_root)
    if not isinstance(value, str) or not value.startswith("./"):
        raise PluginPackageError("plugin MCP cwd must be package-relative")
    return str(resolve_manifest_path(package_root, value, allow_directory=True))


def _require_permission(manifest: PluginManifest, permission: str, identity: str) -> None:
    if not any(item.id == permission for item in manifest.permissions):
        raise PluginPackageError(f"MCP server {identity} requires manifest permission {permission}")


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PluginPackageError(f"plugin MCP {field} must be an object")
    return {str(key): item for key, item in value.items()}


def _string_list(value: object, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PluginPackageError(f"plugin MCP {field} must be a string array")
    return list(value)
