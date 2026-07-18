from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.hooks import HookName
from ouro.core.tools import (
    PermissionMode,
    ToolBatchBehavior,
    ToolEffect,
    ToolPermissionControl,
)
from ouro.plugins.semver import SemVer, validate_version_range

PluginCapability = Literal[
    "tools",
    "commands",
    "hooks",
    "context_providers",
    "compaction_strategies",
    "skills",
    "mcp_servers",
    "background_services",
    "continuation_policies",
    "turn_policies",
    "tool_policies",
]
PluginRuntimeKind = Literal["declarative", "python-inprocess", "worker"]
PLUGIN_PERMISSION_IDS = frozenset(
    {
        "background.run",
        "credentials.use",
        "network.outbound",
        "process.spawn",
        "sessions.write",
    }
)
PluginStatus = Literal[
    "invalid",
    "incompatible",
    "disabled",
    "needs_configuration",
    "needs_code_trust",
    "needs_authorization",
    "ready",
    "starting",
    "active",
    "degraded",
    "blocked",
    "stopping",
    "failed",
]

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_PLUGIN_ID = re.compile(
    r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+\Z",
    re.IGNORECASE,
)
_EXPOSED_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z")


class PermissionRequest(OuroModel):
    id: str
    scopes: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("id")
    @classmethod
    def _validate_permission_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        parts = normalized.split(".")
        if len(parts) < 2 or any(_IDENTIFIER.fullmatch(part) is None for part in parts):
            raise ValueError("permission id must be a dotted stable identifier")
        if normalized not in PLUGIN_PERMISSION_IDS:
            raise ValueError(f"unknown plugin permission id: {normalized}")
        return normalized

    @field_validator("scopes")
    @classmethod
    def _normalize_scopes(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("permission scopes cannot be empty")
        return list(dict.fromkeys(normalized))


class PluginEngines(OuroModel):
    ouro: str
    plugin_api: str

    @field_validator("ouro", "plugin_api")
    @classmethod
    def _validate_range(cls, value: str) -> str:
        return validate_version_range(value)


class PluginRuntimeSpec(OuroModel):
    kind: PluginRuntimeKind = "declarative"
    entrypoint: str | None = None
    command: list[str] = Field(default_factory=list)
    protocol: str = "1.0"

    @model_validator(mode="after")
    def _validate_runtime(self) -> PluginRuntimeSpec:
        if self.kind == "declarative":
            if self.entrypoint is not None or self.command:
                raise ValueError("declarative runtime cannot declare entrypoint or command")
        elif self.kind == "python-inprocess":
            if self.entrypoint is None:
                raise ValueError("python-inprocess runtime requires entrypoint")
            if self.command:
                raise ValueError("python-inprocess runtime cannot declare command")
            _validate_entrypoint(self.entrypoint)
        elif not self.command:
            raise ValueError("worker runtime requires command")
        SemVer.parse(f"{self.protocol}.0" if self.protocol.count(".") == 1 else self.protocol)
        return self


class ToolContributionSpec(OuroModel):
    id: str
    exposed_name: str
    description: str
    permission_mode: PermissionMode = "approval"
    permission_control: ToolPermissionControl = "host"
    effect: ToolEffect = "unknown"
    batch_behavior: ToolBatchBehavior = "compatible"
    resumable: bool = False
    optional: bool = False

    @model_validator(mode="after")
    def validate_permission_control(self) -> ToolContributionSpec:
        if self.permission_control == "tool" and self.permission_mode == "approval":
            raise ValueError("tool-controlled permission must be auto or deny")
        return self

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_local_id(value)

    @field_validator("exposed_name")
    @classmethod
    def _validate_exposed_name(cls, value: str) -> str:
        if _EXPOSED_TOOL.fullmatch(value) is None:
            raise ValueError("tool exposed_name is invalid")
        return value


class CommandContributionSpec(OuroModel):
    id: str
    description: str
    optional: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_local_id(value)


class HookContributionSpec(OuroModel):
    id: str
    event: HookName
    priority: int = Field(default=100, ge=-10_000, le=10_000)
    timeout_seconds: float | None = Field(default=None, gt=0, le=30)
    optional: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_local_id(value)


class ContinuationPolicyContributionSpec(OuroModel):
    id: str
    priority: int = Field(default=100, ge=-10_000, le=10_000)
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    optional: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_local_id(value)


class ToolPolicyContributionSpec(OuroModel):
    id: str
    priority: int = Field(default=100, ge=-10_000, le=10_000)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    optional: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_local_id(value)


class ContextProviderContributionSpec(OuroModel):
    id: str
    priority: int = Field(default=100, ge=-10_000, le=10_000)
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    failure_mode: Literal["omit", "block"] = "omit"
    optional: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_local_id(value)


class TurnPolicyContributionSpec(OuroModel):
    id: str
    priority: int = Field(default=100, ge=-10_000, le=10_000)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    optional: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_local_id(value)


class NamedContributionSpec(OuroModel):
    id: str
    optional: bool = False

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return _validate_local_id(value)


class PathContributionSpec(NamedContributionSpec):
    path: str


class PluginContributes(OuroModel):
    tools: list[ToolContributionSpec] = Field(default_factory=list)
    commands: list[CommandContributionSpec] = Field(default_factory=list)
    hooks: list[HookContributionSpec] = Field(default_factory=list)
    context_providers: list[ContextProviderContributionSpec] = Field(default_factory=list)
    compaction_strategies: list[NamedContributionSpec] = Field(default_factory=list)
    skills: list[PathContributionSpec] = Field(default_factory=list)
    mcp_servers: list[PathContributionSpec] = Field(default_factory=list)
    background_services: list[NamedContributionSpec] = Field(default_factory=list)
    continuation_policies: list[ContinuationPolicyContributionSpec] = Field(default_factory=list)
    turn_policies: list[TurnPolicyContributionSpec] = Field(default_factory=list)
    tool_policies: list[ToolPolicyContributionSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> PluginContributes:
        for kind, items in self.by_kind().items():
            identities = [item.id for item in items]
            if len(identities) != len(set(identities)):
                raise ValueError(f"duplicate {kind} contribution id")
        exposed_names = [item.exposed_name for item in self.tools]
        if len(exposed_names) != len(set(exposed_names)):
            raise ValueError("duplicate tool exposed_name")
        return self

    def by_kind(self) -> dict[PluginCapability, list[Any]]:
        return {
            "tools": list(self.tools),
            "commands": list(self.commands),
            "hooks": list(self.hooks),
            "context_providers": list(self.context_providers),
            "compaction_strategies": list(self.compaction_strategies),
            "skills": list(self.skills),
            "mcp_servers": list(self.mcp_servers),
            "background_services": list(self.background_services),
            "continuation_policies": list(self.continuation_policies),
            "turn_policies": list(self.turn_policies),
            "tool_policies": list(self.tool_policies),
        }

    def capabilities(self) -> set[PluginCapability]:
        return {kind for kind, values in self.by_kind().items() if values}


class PluginConfigurationSpec(OuroModel):
    schema_path: str = Field(alias="schema")
    version: int = Field(default=1, ge=1)


class PluginDependencySpec(OuroModel):
    plugin_id: str
    version: str

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return validate_plugin_id(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        return validate_version_range(value)


class PluginDependencies(OuroModel):
    plugins: list[PluginDependencySpec] = Field(default_factory=list)


class PluginManifest(OuroModel):
    schema_version: Literal[2] = 2
    plugin_id: str
    name: str = Field(min_length=1, max_length=120)
    version: str
    description: str = Field(default="", max_length=1000)
    namespace: str
    engines: PluginEngines
    runtime: PluginRuntimeSpec = Field(default_factory=PluginRuntimeSpec)
    contributes: PluginContributes = Field(default_factory=PluginContributes)
    permissions: list[PermissionRequest] = Field(default_factory=list)
    configuration: PluginConfigurationSpec | None = None
    dependencies: PluginDependencies = Field(default_factory=PluginDependencies)
    metadata: Metadata = MetadataField

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        return validate_plugin_id(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        SemVer.parse(value)
        return value

    @field_validator("namespace")
    @classmethod
    def _validate_namespace(cls, value: str) -> str:
        normalized = value.strip().lower()
        if _IDENTIFIER.fullmatch(normalized) is None:
            raise ValueError("namespace must be a lowercase stable identifier")
        return normalized

    @model_validator(mode="after")
    def _validate_permissions(self) -> PluginManifest:
        identities = [request.id for request in self.permissions]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate permission request id")
        dependencies = [item.plugin_id for item in self.dependencies.plugins]
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("duplicate plugin dependency id")
        if self.plugin_id in dependencies:
            raise ValueError("plugin cannot depend on itself")
        if self.runtime.kind == "python-inprocess":
            assert self.runtime.entrypoint is not None
            top_level = self.runtime.entrypoint.partition(":")[0].partition(".")[0]
            expected = plugin_module_namespace(self.namespace)
            if top_level != expected:
                raise ValueError(
                    f"python entrypoint must use the plugin-owned top-level module {expected!r}"
                )
        return self


def validate_plugin_id(value: str) -> str:
    normalized = value.strip().lower()
    if _PLUGIN_ID.fullmatch(normalized) is None:
        raise ValueError("plugin_id must be a reverse-domain-style identifier")
    return normalized


def plugin_module_namespace(namespace: str) -> str:
    return namespace if namespace.endswith("_plugin") else f"{namespace}_plugin"


def _validate_local_id(value: str) -> str:
    normalized = value.strip().lower()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError("contribution id must be a lowercase stable identifier")
    return normalized


def _validate_entrypoint(value: str) -> None:
    module, separator, factory = value.partition(":")
    if not separator or not module or not factory:
        raise ValueError("entrypoint must use module:factory")
    if any(_IDENTIFIER.fullmatch(part) is None for part in module.split(".")):
        raise ValueError("entrypoint module is invalid")
    if _IDENTIFIER.fullmatch(factory) is None:
        raise ValueError("entrypoint factory is invalid")
