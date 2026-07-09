from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

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


class PluginHookSpec(OuroModel):
    hook_name: HookName
    priority: int = 100
    permissions_required: list[str] = Field(default_factory=list)


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
