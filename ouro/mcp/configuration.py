from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ouro.config.file import JsonConfigFile
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.mcp.models import MCPServerConfig


class MCPConfigSnapshot(OuroModel):
    servers: tuple[MCPServerConfig, ...] = ()
    config_path: Path


class MCPConfigStore:
    """Persist the canonical ``mcp.servers`` configuration section atomically."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root)

    @property
    def config_path(self) -> Path:
        return self._file.path

    def snapshot(self) -> MCPConfigSnapshot:
        servers = self._servers_from_raw(self._file.read())
        return MCPConfigSnapshot(
            servers=tuple(servers[server_id] for server_id in sorted(servers)),
            config_path=self.config_path,
        )

    def upsert(self, config: MCPServerConfig) -> MCPServerConfig:
        raw = self._file.read()
        servers = self._servers_from_raw(raw)
        servers[config.server_id] = config
        self._write(raw, servers)
        return config

    def remove(self, server_id: str) -> MCPServerConfig:
        raw = self._file.read()
        servers = self._servers_from_raw(raw)
        try:
            removed = servers.pop(server_id)
        except KeyError as exc:
            raise ConfigError(f"MCP server not found: {server_id}") from exc
        self._write(raw, servers)
        return removed

    def _servers_from_raw(self, raw: Mapping[str, Any]) -> dict[str, MCPServerConfig]:
        section = raw.get("mcp", {})
        if not isinstance(section, Mapping):
            raise ConfigError("mcp config must be an object")
        unknown = set(section) - {"servers"}
        if unknown:
            raise ConfigError(f"unsupported mcp config fields: {sorted(unknown)}")
        items = section.get("servers", {})
        if not isinstance(items, Mapping):
            raise ConfigError("mcp.servers must be an object")
        servers: dict[str, MCPServerConfig] = {}
        for server_id, value in items.items():
            if not isinstance(server_id, str) or not isinstance(value, Mapping):
                raise ConfigError("each MCP server config must be an object keyed by id")
            payload = dict(value)
            configured_id = payload.get("server_id")
            if configured_id is not None and configured_id != server_id:
                raise ConfigError(f"MCP server_id must match registry key: {server_id}")
            payload["server_id"] = server_id
            try:
                servers[server_id] = MCPServerConfig.model_validate(payload)
            except ValidationError as exc:
                raise ConfigError(f"invalid MCP server config {server_id}: {exc}") from exc
        return servers

    def _write(
        self,
        raw: Mapping[str, Any],
        servers: Mapping[str, MCPServerConfig],
    ) -> None:
        updated = dict(raw)
        updated["mcp"] = {
            "servers": {
                server_id: config.model_dump(
                    mode="json",
                    exclude={"server_id"},
                    exclude_defaults=True,
                )
                for server_id, config in sorted(servers.items())
            }
        }
        self._file.write(updated)


def mcp_config_to_public_dict(config: MCPServerConfig) -> dict[str, Any]:
    public = config.model_dump(mode="json")
    public["credential_refs"] = {
        target: _public_credential_reference(reference)
        for target, reference in config.credential_refs.items()
    }
    public["env"] = dict(config.env)
    public["headers"] = dict(config.headers)
    return public


def _public_credential_reference(reference: str) -> str:
    if reference.startswith("env:"):
        return reference
    return "inline:***"
