from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from tempfile import TemporaryFile
from typing import Any, TextIO, cast

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from ouro.core.redaction import redact_text
from ouro.mcp.client import (
    CredentialResolver,
    MCPConnectionLostHandler,
    MCPDiagnosticHandler,
    MCPToolsChangedHandler,
)
from ouro.mcp.errors import (
    MCPConfigurationError,
    MCPConnectionError,
    MCPDisconnectedError,
    MCPProtocolError,
    MCPTimeoutError,
)
from ouro.mcp.models import (
    MCPRemoteTool,
    MCPRemoteToolResult,
    MCPServerCapabilities,
    MCPServerConfig,
    MCPServerInfo,
)
from ouro.mcp.sse_transport import legacy_sse_transport


class SDKMCPClientSession:
    """Translate the official SDK session into OURO-owned MCP records."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def initialize(self) -> MCPServerInfo:
        try:
            result = await self._session.initialize()
        except Exception as exc:  # noqa: BLE001 - SDK exceptions are isolated here.
            raise _translate_sdk_error(exc, operation="initialize") from exc
        capabilities = result.capabilities
        return MCPServerInfo(
            name=result.serverInfo.name,
            version=result.serverInfo.version,
            protocol_version=str(result.protocolVersion),
            instructions=result.instructions,
            capabilities=MCPServerCapabilities(
                tools=capabilities.tools is not None,
                tools_list_changed=bool(capabilities.tools and capabilities.tools.listChanged),
                resources=capabilities.resources is not None,
                prompts=capabilities.prompts is not None,
                logging=capabilities.logging is not None,
                experimental=dict(capabilities.experimental or {}),
            ),
        )

    async def list_tools(self, cursor: str | None = None) -> tuple[list[MCPRemoteTool], str | None]:
        try:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await self._session.list_tools(params=params)
        except Exception as exc:  # noqa: BLE001
            raise _translate_sdk_error(exc, operation="tools/list") from exc
        return ([_remote_tool(tool) for tool in result.tools], result.nextCursor)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> MCPRemoteToolResult:
        try:
            result = await self._session.call_tool(name, arguments=dict(arguments))
        except Exception as exc:  # noqa: BLE001
            raise _translate_sdk_error(exc, operation=f"tools/call {name}") from exc
        return _remote_result(result)

    async def ping(self) -> None:
        try:
            await self._session.send_ping()
        except Exception as exc:  # noqa: BLE001
            raise _translate_sdk_error(exc, operation="ping") from exc


class SDKMCPClientFactory:
    """Own official SDK transports without exposing SDK objects to OURO callers."""

    @asynccontextmanager
    async def open(
        self,
        config: MCPServerConfig,
        *,
        resolve_credential: CredentialResolver,
        diagnostic: MCPDiagnosticHandler,
        tools_changed: MCPToolsChangedHandler,
        connection_lost: MCPConnectionLostHandler,
    ) -> AsyncGenerator[SDKMCPClientSession, None]:
        secrets = _resolve_secrets(config, resolve_credential)

        async def logging_callback(params: types.LoggingMessageNotificationParams) -> None:
            message = _safe_text(params.data)
            if params.logger:
                message = f"{params.logger}: {message}"
            diagnostic(_diagnostic_level(params.level), _redact(message, secrets.values()))

        async def message_handler(message: object) -> None:
            if isinstance(message, types.ServerNotification) and isinstance(
                message.root, types.ToolListChangedNotification
            ):
                tools_changed()
            elif isinstance(message, Exception):
                error = _translate_sdk_error(message, operation="session transport")
                connection_lost(_redact(str(error), secrets.values()))

        if config.transport == "stdio":
            environment = {
                name: os.environ[name] for name in config.inherit_env if name in os.environ
            }
            environment.update(config.env)
            environment.update(secrets)
            parameters = StdioServerParameters(
                command=config.command or "",
                args=config.args,
                env=environment,
                cwd=config.cwd,
            )
            with TemporaryFile(mode="w+", encoding="utf-8") as errlog:
                try:
                    async with stdio_client(parameters, errlog=cast(TextIO, errlog)) as streams:
                        async with _client_session(
                            streams,
                            config=config,
                            logging_callback=logging_callback,
                            message_handler=message_handler,
                        ) as session:
                            yield SDKMCPClientSession(session)
                except Exception as exc:
                    raise _translate_sdk_error(exc, operation="stdio transport") from exc
                finally:
                    errlog.flush()
                    errlog.seek(0)
                    for line in errlog:
                        message = line.strip()
                        if message:
                            diagnostic("info", _redact(message, secrets.values()))
            return

        headers = dict(config.headers)
        headers.update(secrets)
        read_timeout = None if config.transport == "sse" else config.timeout_seconds
        timeout = httpx.Timeout(config.timeout_seconds, read=read_timeout)
        try:
            # MCP transport configuration is authoritative.  Implicit OS/env
            # proxies can route even loopback servers through an unrelated
            # process and may expose configured credential headers.
            async with httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                trust_env=False,
            ) as http_client:
                if config.transport == "http":
                    async with streamable_http_client(
                        config.url or "",
                        http_client=http_client,
                        terminate_on_close=True,
                    ) as streams:
                        async with _client_session(
                            streams[:2],
                            config=config,
                            logging_callback=logging_callback,
                            message_handler=message_handler,
                        ) as session:
                            yield SDKMCPClientSession(session)
                else:
                    # SSE is retained only for compatibility with legacy servers.
                    async with legacy_sse_transport(
                        config.url or "",
                        client=http_client,
                        timeout_seconds=config.timeout_seconds,
                    ) as streams:
                        async with _client_session(
                            streams,
                            config=config,
                            logging_callback=logging_callback,
                            message_handler=message_handler,
                        ) as session:
                            yield SDKMCPClientSession(session)
        except Exception as exc:
            raise _translate_sdk_error(exc, operation=f"{config.transport} transport") from exc


@asynccontextmanager
async def _client_session(
    streams: tuple[Any, Any],
    *,
    config: MCPServerConfig,
    logging_callback: Any,
    message_handler: Any,
) -> AsyncGenerator[ClientSession, None]:
    session = ClientSession(
        streams[0],
        streams[1],
        read_timeout_seconds=timedelta(seconds=config.timeout_seconds),
        logging_callback=logging_callback,
        message_handler=message_handler,
        client_info=types.Implementation(name="ouro-agent", version="0.1.0"),
    )
    async with session:
        yield session


def _resolve_secrets(
    config: MCPServerConfig,
    resolver: CredentialResolver,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for target, reference in config.credential_refs.items():
        if not target or "\n" in target or "\r" in target:
            raise MCPConfigurationError("credential target is invalid")
        value = resolver(reference)
        if not value:
            raise MCPConfigurationError(f"credential reference is unresolved for {target}")
        resolved[target] = value
    return resolved


def _remote_tool(tool: types.Tool) -> MCPRemoteTool:
    annotations = (
        tool.annotations.model_dump(by_alias=True, exclude_none=True)
        if tool.annotations is not None
        else {}
    )
    return MCPRemoteTool(
        name=tool.name,
        title=tool.title,
        description=tool.description or "",
        input_schema=dict(tool.inputSchema),
        output_schema=dict(tool.outputSchema or {}),
        annotations=annotations,
    )


def _remote_result(result: types.CallToolResult) -> MCPRemoteToolResult:
    text_parts: list[str] = []
    non_text_parts: list[dict[str, Any]] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            text_parts.append(block.text)
        else:
            dumped = block.model_dump(by_alias=True, mode="json", exclude_none=True)
            if isinstance(dumped, dict):
                # Binary payloads stay inside the protocol boundary but are not duplicated
                # into the model-readable text field.
                if dumped.get("type") in {"image", "audio"} and "data" in dumped:
                    dumped["data"] = "<binary omitted>"
                non_text_parts.append(dumped)
    content = "\n".join(text_parts)
    if non_text_parts:
        rendered = json.dumps(non_text_parts, ensure_ascii=False, separators=(",", ":"))
        content = f"{content}\n{rendered}" if content else rendered
    return MCPRemoteToolResult(
        is_error=result.isError,
        content=content,
        structured=dict(result.structuredContent or {}),
        metadata={"content_block_count": len(result.content)},
    )


def _translate_sdk_error(exc: Exception, *, operation: str) -> Exception:
    if isinstance(
        exc,
        (MCPConfigurationError, MCPConnectionError, MCPProtocolError, MCPTimeoutError),
    ):
        return exc
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return MCPTimeoutError(f"MCP {operation} timed out")
    if isinstance(exc, McpError):
        message = redact_text(str(exc))
        lowered = message.lower()
        if "timed out" in lowered or "timeout" in lowered:
            return MCPTimeoutError(f"MCP {operation} timed out")
        if "connection closed" in lowered or "disconnected" in lowered:
            return MCPDisconnectedError(f"MCP {operation} failed: connection closed")
        return MCPProtocolError(f"MCP {operation} failed: {message}")
    if isinstance(exc, (httpx.RequestError, OSError)):
        return MCPConnectionError(f"MCP {operation} failed: {redact_text(str(exc))}")
    return MCPConnectionError(f"MCP {operation} failed: {redact_text(str(exc))}")


def _safe_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - json default handles normal objects.
        return str(value)


def _redact(message: str, secrets: Any) -> str:
    redacted = message
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


def _diagnostic_level(level: types.LoggingLevel) -> str:
    if level in {"error", "critical", "alert", "emergency"}:
        return "error"
    if level == "warning":
        return "warning"
    if level == "debug":
        return "debug"
    return "info"
