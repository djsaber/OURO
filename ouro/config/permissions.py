from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ouro.config.file import JsonConfigFile
from ouro.core.errors import ConfigError
from ouro.core.tools import PermissionMode


@dataclass(frozen=True)
class PermissionConfigSnapshot:
    tools: dict[str, PermissionMode]
    config_path: Path

    def mode_for(self, tool_name: str, default: PermissionMode = "auto") -> PermissionMode:
        return self.tools.get(tool_name, default)


class PermissionConfigManager:
    """Manage persisted tool permission overrides."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root)
        self.root = self._file.root
        self.config_path = self._file.path

    def snapshot(self) -> PermissionConfigSnapshot:
        raw = self.load_raw()
        return PermissionConfigSnapshot(
            tools=_tools_from_raw(raw),
            config_path=self.config_path,
        )

    def set_tool_mode(self, tool_name: str, mode: PermissionMode) -> PermissionConfigSnapshot:
        if mode not in ("auto", "approval"):
            raise ConfigError(f"invalid permission mode: {mode}")
        raw = self.load_raw()
        next_raw = deepcopy(raw)
        permissions = next_raw.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        tools = permissions.get("tools")
        if not isinstance(tools, dict):
            tools = {}
        tools[tool_name] = mode
        permissions["tools"] = tools
        next_raw["permissions"] = permissions
        self._write_raw(next_raw)
        return self.snapshot()

    def clear_tool_mode(self, tool_name: str) -> PermissionConfigSnapshot:
        raw = self.load_raw()
        next_raw = deepcopy(raw)
        permissions = next_raw.get("permissions")
        if isinstance(permissions, dict):
            tools = permissions.get("tools")
            if isinstance(tools, dict):
                tools.pop(tool_name, None)
                if tools:
                    permissions["tools"] = tools
                else:
                    permissions.pop("tools", None)
                if permissions:
                    next_raw["permissions"] = permissions
                else:
                    next_raw.pop("permissions", None)
        self._write_raw(next_raw)
        return self.snapshot()

    def load_raw(self) -> dict[str, Any]:
        return self._file.read()

    def _write_raw(self, raw: Mapping[str, Any]) -> None:
        self._file.write(raw)


def _tools_from_raw(raw: Mapping[str, Any]) -> dict[str, PermissionMode]:
    result: dict[str, PermissionMode] = {}
    permissions = raw.get("permissions")
    if not isinstance(permissions, Mapping):
        return result
    tools = permissions.get("tools")
    if not isinstance(tools, Mapping):
        return result
    for name, mode in tools.items():
        if mode in ("auto", "approval"):
            result[str(name)] = mode
    return result


def permission_config_to_public_dict(snapshot: PermissionConfigSnapshot) -> dict[str, Any]:
    return {
        "tools": snapshot.tools,
        "config_path": str(snapshot.config_path),
    }
