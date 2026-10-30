from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.core.tools import PermissionMode


class PermissionConfig(OuroModel):
    tools: dict[str, PermissionMode] = Field(default_factory=dict)
    plugins: dict[str, PermissionMode] = Field(default_factory=dict)


@dataclass(frozen=True)
class PermissionConfigSnapshot:
    tools: dict[str, PermissionMode]
    plugins: dict[str, PermissionMode]
    config_path: Path


class PermissionConfigManager:
    """Manage persisted tool permission overrides."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root)
        self.root = self._file.root
        self.config_path = self._file.path

    def snapshot(self) -> PermissionConfigSnapshot:
        raw = self.load_raw()
        config = _permission_config_from_raw(raw)
        return PermissionConfigSnapshot(
            tools=config.tools,
            plugins=config.plugins,
            config_path=self.config_path,
        )

    @atomic_config_update
    def set_tool_mode(self, tool_name: str, mode: PermissionMode) -> PermissionConfigSnapshot:
        if mode not in ("auto", "approval", "deny"):
            raise ConfigError(f"invalid permission mode: {mode}")
        raw = self.load_raw()
        config = _permission_config_from_raw(raw)
        next_raw = deepcopy(raw)
        tools = dict(config.tools)
        tools[tool_name] = mode
        next_raw["permissions"] = _permission_section(tools=tools, plugins=config.plugins)
        self._write_raw(next_raw)
        return self.snapshot()

    @atomic_config_update
    def clear_tool_mode(self, tool_name: str) -> PermissionConfigSnapshot:
        raw = self.load_raw()
        config = _permission_config_from_raw(raw)
        next_raw = deepcopy(raw)
        tools = dict(config.tools)
        tools.pop(tool_name, None)
        if tools or config.plugins:
            next_raw["permissions"] = _permission_section(
                tools=tools,
                plugins=config.plugins,
            )
        else:
            next_raw.pop("permissions", None)
        self._write_raw(next_raw)
        return self.snapshot()

    @atomic_config_update
    def set_plugin_mode(self, plugin_id: str, mode: PermissionMode) -> PermissionConfigSnapshot:
        if not plugin_id:
            raise ConfigError("plugin id cannot be empty")
        if mode not in ("auto", "approval", "deny"):
            raise ConfigError(f"invalid permission mode: {mode}")
        raw = self.load_raw()
        config = _permission_config_from_raw(raw)
        next_raw = deepcopy(raw)
        plugins = dict(config.plugins)
        plugins[plugin_id] = mode
        next_raw["permissions"] = _permission_section(tools=config.tools, plugins=plugins)
        self._write_raw(next_raw)
        return self.snapshot()

    @atomic_config_update
    def clear_plugin_mode(self, plugin_id: str) -> PermissionConfigSnapshot:
        raw = self.load_raw()
        config = _permission_config_from_raw(raw)
        next_raw = deepcopy(raw)
        plugins = dict(config.plugins)
        plugins.pop(plugin_id, None)
        if config.tools or plugins:
            next_raw["permissions"] = _permission_section(
                tools=config.tools,
                plugins=plugins,
            )
        else:
            next_raw.pop("permissions", None)
        self._write_raw(next_raw)
        return self.snapshot()

    def load_raw(self) -> dict[str, Any]:
        return self._file.read()

    def _write_raw(self, raw: Mapping[str, Any]) -> None:
        self._file.write(raw)


def _permission_config_from_raw(raw: Mapping[str, Any]) -> PermissionConfig:
    section = raw.get("permissions", {})
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ConfigError("permissions config must be an object")
    try:
        return PermissionConfig.model_validate(dict(section))
    except ValidationError as exc:
        raise ConfigError(f"invalid permissions config: {exc}") from exc


def permission_config_to_public_dict(snapshot: PermissionConfigSnapshot) -> dict[str, Any]:
    return {
        "tools": snapshot.tools,
        "plugins": snapshot.plugins,
        "config_path": str(snapshot.config_path),
    }


def _permission_section(
    *,
    tools: Mapping[str, PermissionMode],
    plugins: Mapping[str, PermissionMode],
) -> dict[str, dict[str, PermissionMode]]:
    section: dict[str, dict[str, PermissionMode]] = {}
    if tools:
        section["tools"] = dict(tools)
    if plugins:
        section["plugins"] = dict(plugins)
    return section
