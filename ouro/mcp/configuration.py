from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, ValidationError

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.core.base import OuroModel, new_id
from ouro.core.errors import ConfigError
from ouro.core.redaction import public_url
from ouro.core.tools import PermissionMode, ToolEffect
from ouro.mcp.models import MCPServerConfig, MCPTransport


class MCPConfigSnapshot(OuroModel):
    servers: tuple[MCPServerConfig, ...] = ()
    config_path: Path


class MCPServerConfigUpdate(OuroModel):
    """Fields supplied by a configuration client for one atomic update."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    transport: MCPTransport | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    inherit_env: list[str] | None = None
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    credential_refs: dict[str, str] | None = None
    enabled: bool | None = None
    auto_start: bool | None = None
    auto_reconnect: bool | None = None
    max_reconnect_attempts: int | None = None
    reconnect_backoff_seconds: float | None = None
    timeout_seconds: float | None = None
    health_check_interval_seconds: float | None = None
    permission_mode: PermissionMode | None = None
    tool_effect_overrides: dict[str, ToolEffect] | None = None


class MCPConfigStore:
    """Persist the canonical ``mcp.servers`` configuration section atomically."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root, name="mcp.json")

    @property
    def config_path(self) -> Path:
        return self._file.path

    def snapshot(self) -> MCPConfigSnapshot:
        servers = self._servers_from_raw(self._file.read())
        return MCPConfigSnapshot(
            servers=tuple(servers[server_id] for server_id in sorted(servers)),
            config_path=self.config_path,
        )

    def get(self, server_id: str) -> MCPServerConfig | None:
        return self._servers_from_raw(self._file.read()).get(server_id)

    def find_by_name(self, name: str) -> MCPServerConfig | None:
        return self._find_by_name(self._servers_from_raw(self._file.read()), name)

    @staticmethod
    def _find_by_name(servers: Mapping[str, MCPServerConfig], name: str) -> MCPServerConfig | None:
        name = name.strip()
        return next((config for config in servers.values() if config.name == name), None)

    @atomic_config_update
    def update(
        self,
        server_id: str | None,
        update: MCPServerConfigUpdate,
    ) -> MCPServerConfig:
        """Create with an allocated identity, or update an existing internal identity."""

        raw = self._file.read()
        servers = self._servers_from_raw(raw)
        return self._save(raw, servers, server_id, update)

    @atomic_config_update
    def set_by_name(self, name: str, update: MCPServerConfigUpdate) -> MCPServerConfig:
        """Atomically create or update the uniquely named server."""

        if not name.strip():
            raise ConfigError("MCP display name must not be blank")
        raw = self._file.read()
        servers = self._servers_from_raw(raw)
        current = self._find_by_name(servers, name)
        if current is None and "name" not in update.model_fields_set:
            update = update.model_copy(update={"name": name})
        return self._save(raw, servers, current.server_id if current else None, update)

    def _save(
        self,
        raw: Mapping[str, Any],
        servers: dict[str, MCPServerConfig],
        server_id: str | None,
        update: MCPServerConfigUpdate,
    ) -> MCPServerConfig:
        payload: dict[str, Any]
        if server_id is None:
            server_id = new_id("mcp")
            payload = {"server_id": server_id, "transport": "stdio"}
        else:
            current = servers.get(server_id)
            if current is None:
                raise ConfigError("MCP server not found")
            payload = current.model_dump(mode="python")

        changes = update.model_dump(
            mode="python",
            include=update.model_fields_set,
        )
        current_transport = payload["transport"]
        target_transport = changes.get("transport", payload["transport"])
        if (current_transport == "stdio") != (target_transport == "stdio"):
            self._reset_transport_fields(payload, target_transport)
        payload.update(changes)

        try:
            config = MCPServerConfig.model_validate(payload)
        except ValidationError as exc:
            raise ConfigError(f"invalid MCP server config: {_validation_summary(exc)}") from exc
        servers[server_id] = config
        self._write(raw, servers)
        return config

    @atomic_config_update
    def remove(self, server_id: str) -> MCPServerConfig:
        raw = self._file.read()
        servers = self._servers_from_raw(raw)
        try:
            removed = servers.pop(server_id)
        except KeyError as exc:
            raise ConfigError("MCP server not found") from exc
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
            if "server_id" in payload:
                raise ConfigError("server_id is internal and cannot be configured")
            payload["server_id"] = server_id
            try:
                servers[server_id] = MCPServerConfig.model_validate(payload)
            except ValidationError as exc:
                raise ConfigError(f"invalid MCP server config: {_validation_summary(exc)}") from exc
        self._validate_names(servers)
        return servers

    @staticmethod
    def _validate_names(servers: Mapping[str, MCPServerConfig]) -> None:
        names: set[str] = set()
        for config in servers.values():
            if config.name in names:
                raise ConfigError(f"MCP display name already exists: {config.name}")
            names.add(config.name)

    @staticmethod
    def _reset_transport_fields(payload: dict[str, Any], transport: MCPTransport) -> None:
        if transport == "stdio":
            payload.update(url=None, headers={}, credential_refs={})
            return
        payload.update(command=None, args=[], env={}, cwd=None, credential_refs={})

    def _write(
        self,
        raw: Mapping[str, Any],
        servers: Mapping[str, MCPServerConfig],
    ) -> None:
        self._validate_names(servers)
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
    public = config.model_dump(mode="json", exclude={"server_id"})
    public["credential_refs"] = {
        target: _public_credential_reference(reference)
        for target, reference in config.credential_refs.items()
    }
    public["args"] = ["***" for _ in config.args]
    public["env"] = {name: "***" for name in config.env}
    public["headers"] = {name: "***" for name in config.headers}
    public["url"] = public_url(config.url) if config.url is not None else None
    return public


def _public_credential_reference(reference: str) -> str:
    if reference.startswith("env:"):
        return reference
    return "inline:***"


def _validation_summary(exc: ValidationError) -> str:
    details: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(item) for item in error["loc"]) or "config"
        details.append(f"{location}: {error['msg']}")
    return "; ".join(details)
