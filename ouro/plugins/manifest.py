from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel
from ouro.core.hooks import HookName, PluginSource

PluginCapability = Literal[
    "tools",
    "commands",
    "context_providers",
    "model_providers",
    "skills",
    "runtime_hooks",
    "mcp_servers",
    "ui_extensions",
    "background_listeners",
    "compaction_strategies",
]
PluginStatus = Literal[
    "discovered",
    "enabled",
    "disabled",
    "failed",
    "needs_authorization",
    "needs_configuration",
]


class PluginPermissions(OuroModel):
    read_write: bool = False
    network: bool = False

    def missing_from(self, grant: PluginPermissions) -> list[str]:
        missing: list[str] = []
        if self.read_write and not grant.read_write:
            missing.append("read_write")
        if self.network and not grant.network:
            missing.append("network")
        return missing


class PluginHookSpec(OuroModel):
    hook_name: HookName
    priority: int = 100
    permissions_required: list[Literal["read_write", "network"]] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0, le=30)


class PluginManifest(OuroModel):
    schema_version: int = SCHEMA_VERSION
    plugin_id: str
    name: str
    version: str
    source_type: PluginSource = "third_party"
    entrypoint: str | None = None
    description: str = ""
    capabilities: list[PluginCapability] = Field(default_factory=list)
    permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    commands: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    runtime_hooks: list[PluginHookSpec] = Field(default_factory=list)
    metadata: Metadata = MetadataField

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        normalized = value.strip()
        parts = normalized.split(".")
        if len(parts) < 2 or any(
            not part
            or not part[0].isalpha()
            or any(not (char.isalnum() or char in {"_", "-"}) for char in part)
            for part in parts
        ):
            raise ValueError("plugin_id must be a reverse-domain-style identifier")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def _deduplicate_capabilities(
        cls,
        value: list[PluginCapability],
    ) -> list[PluginCapability]:
        return list(dict.fromkeys(value))
