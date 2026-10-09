from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.tools import PermissionMode, ToolEffect

MCPTransport = Literal["stdio", "http", "sse"]
MCPServerStatus = Literal[
    "configured",
    "starting",
    "connected",
    "reconnecting",
    "failed",
    "stopping",
    "stopped",
    "disabled",
]
MCPDiagnosticLevel = Literal["debug", "info", "warning", "error"]

ServerId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class MCPServerConfig(OuroModel):
    """Validated, durable configuration for one MCP server."""

    server_id: ServerId
    name: str
    transport: MCPTransport = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    inherit_env: list[str] = Field(
        default_factory=lambda: [
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
        ]
    )
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    credential_refs: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    auto_start: bool = False
    auto_reconnect: bool = True
    max_reconnect_attempts: int = Field(default=3, ge=0, le=20)
    reconnect_backoff_seconds: float = Field(default=0.5, gt=0, le=300)
    timeout_seconds: float = Field(default=30.0, gt=0, le=3600)
    health_check_interval_seconds: float = Field(default=30.0, gt=0, le=3600)
    permission_mode: PermissionMode = "approval"
    tool_effect_overrides: dict[str, ToolEffect] = Field(default_factory=dict)
    metadata: Metadata = MetadataField

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("command", "cwd", "url", mode="before")
    @classmethod
    def _blank_optional_string_is_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("inherit_env")
    @classmethod
    def _validate_inherited_environment(cls, value: list[str]) -> list[str]:
        if any(not _ENVIRONMENT_NAME.fullmatch(name) for name in value):
            raise ValueError("inherit_env entries must be environment variable names")
        return list(dict.fromkeys(value))

    @field_validator("env")
    @classmethod
    def _validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not _ENVIRONMENT_NAME.fullmatch(name) for name in value):
            raise ValueError("env keys must be environment variable names")
        if any("\x00" in item for item in value.values()):
            raise ValueError("env values must not contain NUL")
        return value

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not _HEADER_NAME.fullmatch(name) for name in value):
            raise ValueError("header names are invalid")
        if any("\r" in item or "\n" in item for item in value.values()):
            raise ValueError("header values must not contain newlines")
        return value

    @field_validator("tool_effect_overrides")
    @classmethod
    def _validate_tool_effect_overrides(
        cls,
        value: dict[str, ToolEffect],
    ) -> dict[str, ToolEffect]:
        if any(not name.strip() for name in value):
            raise ValueError("tool effect override names must not be blank")
        return {name.strip(): effect for name, effect in value.items()}

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> MCPServerConfig:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("command is required for stdio transport")
            if self.url is not None:
                raise ValueError("url is not valid for stdio transport")
            if self.headers:
                raise ValueError("headers are not valid for stdio transport")
        else:
            if not self.url:
                raise ValueError(f"url is required for {self.transport} transport")
            parsed_url = urlsplit(self.url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                raise ValueError("MCP URL must use http or https")
            if parsed_url.username is not None or parsed_url.password is not None:
                raise ValueError("MCP URL must not contain credentials")
            if self.command is not None or self.args or self.cwd is not None or self.env:
                raise ValueError(
                    f"command, args, cwd, and env are not valid for {self.transport} transport"
                )
        target_pattern = _ENVIRONMENT_NAME if self.transport == "stdio" else _HEADER_NAME
        if any(not target_pattern.fullmatch(target) for target in self.credential_refs):
            raise ValueError("credential target is invalid for the selected transport")
        if any(not reference for reference in self.credential_refs.values()):
            raise ValueError("credential references must not be blank")
        return self


class MCPServerCapabilities(OuroModel):
    tools: bool = False
    tools_list_changed: bool = False
    resources: bool = False
    prompts: bool = False
    logging: bool = False
    experimental: Metadata = MetadataField


class MCPServerInfo(OuroModel):
    name: str
    version: str
    protocol_version: str
    instructions: str | None = None
    capabilities: MCPServerCapabilities = Field(default_factory=MCPServerCapabilities)


class MCPServerRecord(OuroModel):
    config: MCPServerConfig
    status: MCPServerStatus = "configured"
    server_info: MCPServerInfo | None = None
    tool_names: tuple[str, ...] = ()
    error: str | None = None
    reconnect_count: int = 0
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    metadata: Metadata = MetadataField


class MCPDiagnostic(OuroModel):
    server_id: str
    level: MCPDiagnosticLevel
    message: str
    created_at: datetime


class MCPRemoteTool(OuroModel):
    name: str
    title: str | None = None
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)


class MCPRemoteToolResult(OuroModel):
    is_error: bool = False
    content: str = ""
    structured: dict[str, Any] = Field(default_factory=dict)
    metadata: Metadata = MetadataField
