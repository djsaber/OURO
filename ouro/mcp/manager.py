from __future__ import annotations

import asyncio
import builtins
import os
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field

from ouro.core.base import utc_now
from ouro.core.redaction import redact_text
from ouro.mcp.client import MCPClientFactory, MCPClientSession
from ouro.mcp.errors import (
    MCPDisconnectedError,
    MCPStateError,
    MCPToolCallError,
)
from ouro.mcp.models import (
    MCPDiagnostic,
    MCPDiagnosticLevel,
    MCPRemoteTool,
    MCPServerConfig,
    MCPServerRecord,
    MCPServerStatus,
)
from ouro.mcp.tool_adapter import adapt_mcp_tool, mcp_tool_result
from ouro.tools import (
    Tool,
    ToolExecutionContext,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)

CredentialResolver = Callable[[str], str | None]
_ACTIVE_STATUSES = {"starting", "connected", "reconnecting", "stopping"}


@dataclass
class _ServerRuntime:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    recycle: asyncio.Event = field(default_factory=asyncio.Event)
    tools_changed: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Future[MCPServerRecord] | None = None
    task: asyncio.Task[None] | None = None
    session: MCPClientSession | None = None
    connection_config: MCPServerConfig | None = None
    discovered_tools: tuple[MCPRemoteTool, ...] = ()
    registered_tools: tuple[str, ...] = ()
    recycle_error: str | None = None


class MCPManager:
    """Own MCP server tasks, state transitions, and ToolRegistry projections."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        client_factory: MCPClientFactory | None = None,
        resolve_credential: CredentialResolver | None = None,
        diagnostic_limit: int = 200,
    ) -> None:
        if diagnostic_limit <= 0:
            raise ValueError("diagnostic_limit must be positive")
        self._servers: dict[str, MCPServerRecord] = {}
        self._runtimes: dict[str, _ServerRuntime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry = registry
        self._client_factory = client_factory
        self._resolve_credential = resolve_credential or _default_credential_resolver
        self._diagnostics: dict[str, deque[MCPDiagnostic]] = {}
        self._diagnostic_limit = diagnostic_limit

    async def __aenter__(self) -> MCPManager:
        await self.start_auto()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def configure(self, config: MCPServerConfig) -> MCPServerRecord:
        current = self._servers.get(config.server_id)
        if current is not None and current.status in _ACTIVE_STATUSES:
            raise MCPStateError(f"cannot configure active MCP server {config.name}; stop it first")
        status: MCPServerStatus = "configured" if config.enabled else "disabled"
        record = MCPServerRecord(config=config, status=status)
        self._servers[config.server_id] = record
        self._locks.setdefault(config.server_id, asyncio.Lock())
        self._diagnostics.setdefault(config.server_id, deque(maxlen=self._diagnostic_limit))
        return record

    def configure_many(self, configs: Iterable[MCPServerConfig]) -> builtins.list[MCPServerRecord]:
        configs = builtins.list(configs)
        ids = [config.server_id for config in configs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate MCP server ids")
        for config in configs:
            current = self._servers.get(config.server_id)
            if current is not None and current.status in _ACTIVE_STATUSES:
                raise MCPStateError(
                    f"cannot configure active MCP server {config.name}; stop it first"
                )
        return [self.configure(config) for config in configs]

    def apply_config(self, config: MCPServerConfig) -> MCPServerRecord:
        """Reconcile saved configuration with this process-owned runtime.

        An inactive server can safely adopt the complete configuration. An active
        connection keeps its lifecycle settings until restart, while tool policy
        fields are immediately reprojected onto its registered tools.
        """

        current = self._servers.get(config.server_id)
        if current is None or current.status not in _ACTIVE_STATUSES:
            return self.configure(config)

        runtime = self._runtimes.get(config.server_id)
        if runtime is not None and runtime.discovered_tools:
            self._replace_discovered_tools(
                config.server_id,
                runtime,
                list(runtime.discovered_tools),
                config=config,
            )
        self._update(config.server_id, config=config)
        return self.get(config.server_id)

    async def reconcile_config(self, config: MCPServerConfig) -> MCPServerRecord:
        """Reconcile an enabled-state change with the owned connection lifecycle.

        Policy-only changes remain in-place through ``apply_config``. Disabling
        first drains the connection and withdraws its tools; enabling restores
        the configured state and honors auto-start for the live Host.
        """

        current = self._servers.get(config.server_id)
        if current is None:
            return self.configure(config)
        was_enabled = current.config.enabled
        if was_enabled and not config.enabled:
            await self.stop(config.server_id)
            return self.configure(config)

        record = self.apply_config(config)
        if not was_enabled and config.enabled and config.auto_start:
            return await self.start_background(config.server_id)
        return record

    def get(self, server_id: str) -> MCPServerRecord:
        try:
            return self._servers[server_id]
        except KeyError as exc:
            raise KeyError("unknown MCP server") from exc

    def list(self) -> builtins.list[MCPServerRecord]:
        return [self._servers[server_id] for server_id in sorted(self._servers)]

    def diagnostics(self, server_id: str) -> builtins.list[MCPDiagnostic]:
        self.get(server_id)
        return builtins.list(self._diagnostics.get(server_id, ()))

    async def start(self, server_id: str) -> MCPServerRecord:
        record, started = await self._ensure_started(server_id)
        if started is None:
            return record
        await asyncio.shield(started)
        return self.get(server_id)

    async def start_background(self, server_id: str) -> MCPServerRecord:
        """Ensure one connection task exists without waiting for network I/O."""

        record, _ = await self._ensure_started(server_id)
        return record

    async def _ensure_started(
        self,
        server_id: str,
    ) -> tuple[MCPServerRecord, asyncio.Future[MCPServerRecord] | None]:
        record = self.get(server_id)
        if not record.config.enabled:
            return self._update(server_id, status="disabled", error=None), None

        lock = self._locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            runtime = self._runtimes.get(server_id)
            if runtime is not None and runtime.task is not None and not runtime.task.done():
                started = runtime.started
            else:
                loop = asyncio.get_running_loop()
                runtime = _ServerRuntime(started=loop.create_future())
                self._runtimes[server_id] = runtime
                self._update(
                    server_id,
                    status="starting",
                    error=None,
                    server_info=None,
                    tool_names=(),
                    reconnect_count=0,
                    stopped_at=None,
                )
                runtime.task = asyncio.create_task(
                    self._run_server(server_id, runtime),
                    name=f"ouro-mcp-{server_id}",
                )
                started = runtime.started
        assert started is not None
        return self.get(server_id), started

    async def start_auto(self) -> builtins.list[MCPServerRecord]:
        server_ids = [
            record.config.server_id
            for record in self.list()
            if record.config.enabled and record.config.auto_start
        ]
        if not server_ids:
            return []
        return builtins.list(
            await asyncio.gather(*(self.start(server_id) for server_id in server_ids))
        )

    async def start_auto_background(self) -> builtins.list[MCPServerRecord]:
        """Schedule configured auto-start servers and return their live states."""

        server_ids = [
            record.config.server_id
            for record in self.list()
            if record.config.enabled and record.config.auto_start
        ]
        if not server_ids:
            return []
        return builtins.list(
            await asyncio.gather(*(self.start_background(server_id) for server_id in server_ids))
        )

    async def stop(self, server_id: str) -> MCPServerRecord:
        record = self.get(server_id)
        runtime = self._runtimes.get(server_id)
        if runtime is None or runtime.task is None or runtime.task.done():
            if record.status == "disabled":
                return record
            self._unregister_tools(server_id, runtime)
            return self._update(server_id, status="stopped", stopped_at=utc_now())

        self._update(server_id, status="stopping")
        runtime.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(runtime.task), timeout=0.1)
        except TimeoutError:
            runtime.task.cancel()
            with suppress(asyncio.CancelledError):
                await runtime.task
        return self.get(server_id)

    async def restart(self, server_id: str) -> MCPServerRecord:
        await self.stop(server_id)
        return await self.start(server_id)

    async def reconnect(self, server_id: str) -> MCPServerRecord:
        return await self.restart(server_id)

    async def refresh_tools(self, server_id: str) -> tuple[str, ...]:
        runtime = self._connected_runtime(server_id)
        tools = await self._discover_tools(runtime.session)
        return self._replace_discovered_tools(server_id, runtime, tools)

    async def remove(self, server_id: str) -> MCPServerRecord:
        await self.stop(server_id)
        record = self._servers.pop(server_id)
        self._runtimes.pop(server_id, None)
        self._locks.pop(server_id, None)
        self._diagnostics.pop(server_id, None)
        return record

    async def close(self) -> None:
        server_ids = list(self._servers)
        if server_ids:
            await asyncio.gather(*(self.stop(server_id) for server_id in server_ids))

    def register_tools(
        self,
        *,
        server_id: str,
        tools: builtins.list[Tool],
        registry: ToolRegistry,
    ) -> builtins.list[str]:
        """Register explicit MCP tools for compatibility with custom adapters."""

        self.get(server_id)
        adapted: builtins.list[Tool] = []
        for tool in tools:
            metadata = dict(tool.definition.metadata)
            metadata["mcp_server_id"] = server_id
            definition = tool.definition.model_copy(update={"source": "mcp", "metadata": metadata})
            adapted.append(
                Tool(
                    definition=definition,
                    handler=tool.handler,
                    resume_handler=tool.resume_handler,
                )
            )
        registry.replace((), adapted)
        return [tool.definition.name for tool in adapted]

    async def _run_server(self, server_id: str, runtime: _ServerRuntime) -> None:
        config = self.get(server_id).config
        runtime.connection_config = config
        reconnect_attempt = 0
        client_factory = self._client_factory
        try:
            while not runtime.stop.is_set():
                status: MCPServerStatus = "reconnecting" if reconnect_attempt else "starting"
                self._update(server_id, status=status, error=None)
                try:
                    if client_factory is None:
                        client_factory = await asyncio.to_thread(_default_client_factory)
                    if runtime.stop.is_set():
                        break
                    async with client_factory.open(
                        config,
                        resolve_credential=self._resolve_credential,
                        diagnostic=lambda level, message: self._record_diagnostic(
                            server_id, level, message
                        ),
                        tools_changed=runtime.tools_changed.set,
                        connection_lost=lambda error: self._request_recycle(runtime, error),
                    ) as session:
                        runtime.session = session
                        server_info = await asyncio.wait_for(
                            session.initialize(), timeout=config.timeout_seconds
                        )
                        tools = await asyncio.wait_for(
                            self._discover_tools(session), timeout=config.timeout_seconds
                        )
                        tool_names = self._replace_discovered_tools(server_id, runtime, tools)
                        reconnect_attempt = 0
                        record = self._update(
                            server_id,
                            status="connected",
                            server_info=server_info,
                            tool_names=tool_names,
                            error=None,
                            started_at=utc_now(),
                        )
                        if runtime.started is not None and not runtime.started.done():
                            runtime.started.set_result(record)
                        await self._supervise_connection(server_id, runtime, config)
                        if runtime.stop.is_set():
                            break
                        raise MCPDisconnectedError(
                            runtime.recycle_error or "MCP connection must be recycled"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - server failures are isolated.
                    runtime.session = None
                    self._unregister_tools(server_id, runtime)
                    error = self._safe_error(server_id, exc)
                    self._record_diagnostic(server_id, "error", error)
                    if runtime.stop.is_set():
                        break
                    can_reconnect = (
                        config.auto_reconnect and reconnect_attempt < config.max_reconnect_attempts
                    )
                    if not can_reconnect:
                        record = self._update(server_id, status="failed", error=error)
                        if runtime.started is not None and not runtime.started.done():
                            runtime.started.set_result(record)
                        return
                    reconnect_attempt += 1
                    self._update(
                        server_id,
                        status="reconnecting",
                        error=error,
                        reconnect_count=self.get(server_id).reconnect_count + 1,
                    )
                    delay = min(
                        300.0,
                        config.reconnect_backoff_seconds * (2 ** (reconnect_attempt - 1)),
                    )
                    try:
                        await asyncio.wait_for(runtime.stop.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    runtime.recycle.clear()
                    runtime.recycle_error = None
        except asyncio.CancelledError:
            runtime.stop.set()
            raise
        finally:
            runtime.session = None
            runtime.connection_config = None
            self._unregister_tools(server_id, runtime)
            if runtime.started is not None and not runtime.started.done():
                runtime.started.set_result(self.get(server_id))
            if runtime.stop.is_set():
                self._update(
                    server_id,
                    status="stopped",
                    error=None,
                    server_info=None,
                    tool_names=(),
                    stopped_at=utc_now(),
                )

    async def _supervise_connection(
        self,
        server_id: str,
        runtime: _ServerRuntime,
        config: MCPServerConfig,
    ) -> None:
        while not runtime.stop.is_set():
            reason = await _wait_for_runtime_event(
                runtime,
                timeout=config.health_check_interval_seconds,
            )
            if reason == "stop":
                return
            if reason == "recycle":
                return
            if reason == "tools_changed":
                runtime.tools_changed.clear()
                session = runtime.session
                if session is None:
                    return
                tools = await self._discover_tools(session)
                tool_names = self._replace_discovered_tools(server_id, runtime, tools)
                self._update(server_id, tool_names=tool_names)
                continue
            session = runtime.session
            if session is None:
                return
            await session.ping()

    async def _discover_tools(
        self, session: MCPClientSession | None
    ) -> builtins.list[MCPRemoteTool]:
        if session is None:
            raise MCPDisconnectedError("MCP session is not connected")
        tools: builtins.list[MCPRemoteTool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page, next_cursor = await session.list_tools(cursor)
            tools.extend(page)
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise ValueError("MCP tools/list returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tools

    def _replace_discovered_tools(
        self,
        server_id: str,
        runtime: _ServerRuntime,
        remote_tools: builtins.list[MCPRemoteTool],
        *,
        config: MCPServerConfig | None = None,
    ) -> tuple[str, ...]:
        config = config or self.get(server_id).config
        tools = [
            adapt_mcp_tool(config, remote_tool, self._invoke_remote) for remote_tool in remote_tools
        ]
        names = tuple(tool.definition.name for tool in tools)
        if len(names) != len(set(names)):
            raise ValueError(f"MCP server {config.name} returned duplicate tool names")
        if self._registry is not None:
            self._registry.replace(runtime.registered_tools, tools)
        runtime.discovered_tools = tuple(remote_tools)
        runtime.registered_tools = names
        return names

    async def _invoke_remote(
        self,
        server_id: str,
        remote_name: str,
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult:
        runtime = self._connected_runtime(server_id)
        session = runtime.session
        assert session is not None
        try:
            remote_result = await session.call_tool(remote_name, invocation.arguments)
        except Exception as exc:  # noqa: BLE001 - remote call failure is isolated.
            error = self._safe_error(server_id, exc)
            self._record_diagnostic(server_id, "error", error)
            self._request_recycle(runtime, error)
            raise MCPToolCallError(error) from exc
        return mcp_tool_result(invocation, remote_result)

    @staticmethod
    def _request_recycle(runtime: _ServerRuntime, error: str) -> None:
        if runtime.stop.is_set():
            return
        runtime.recycle_error = error
        runtime.recycle.set()

    def _connected_runtime(self, server_id: str) -> _ServerRuntime:
        record = self.get(server_id)
        runtime = self._runtimes.get(server_id)
        if record.status != "connected" or runtime is None or runtime.session is None:
            raise MCPDisconnectedError(f"MCP server is not connected: {record.config.name}")
        return runtime

    def _unregister_tools(
        self,
        server_id: str,
        runtime: _ServerRuntime | None,
    ) -> None:
        if runtime is None or not runtime.registered_tools:
            return
        if self._registry is not None:
            self._registry.unregister(runtime.registered_tools)
        runtime.discovered_tools = ()
        runtime.registered_tools = ()
        if server_id in self._servers:
            self._update(server_id, tool_names=())

    def _record_diagnostic(
        self,
        server_id: str,
        level: str,
        message: str,
    ) -> None:
        normalized: MCPDiagnosticLevel
        if level == "debug":
            normalized = "debug"
        elif level == "warning":
            normalized = "warning"
        elif level == "error":
            normalized = "error"
        else:
            normalized = "info"
        diagnostics = self._diagnostics.setdefault(server_id, deque(maxlen=self._diagnostic_limit))
        diagnostics.append(
            MCPDiagnostic(
                server_id=server_id,
                level=normalized,
                message=message,
                created_at=utc_now(),
            )
        )

    def _safe_error(self, server_id: str, exc: Exception) -> str:
        message = str(exc) or type(exc).__name__
        runtime = self._runtimes.get(server_id)
        config = (
            runtime.connection_config
            if runtime is not None and runtime.connection_config is not None
            else self.get(server_id).config
        )
        for reference in config.credential_refs.values():
            with suppress(Exception):
                secret = self._resolve_credential(reference)
                if secret:
                    message = message.replace(secret, "***")
        return redact_text(message)

    def _update(self, server_id: str, **changes: object) -> MCPServerRecord:
        record = self.get(server_id).model_copy(update=changes)
        self._servers[server_id] = record
        return record


async def _wait_for_runtime_event(
    runtime: _ServerRuntime,
    *,
    timeout: float,
) -> str:
    waiters = {
        asyncio.create_task(runtime.stop.wait()): "stop",
        asyncio.create_task(runtime.recycle.wait()): "recycle",
        asyncio.create_task(runtime.tools_changed.wait()): "tools_changed",
    }
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            return "health_check"
        task = next(task for task in waiters if task in done)
        return waiters[task]
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


def _default_client_factory() -> MCPClientFactory:
    # Loading the SDK belongs to connection startup, not configuration imports
    # or Hosts with no MCP servers. The owner calls this outside the event loop.
    from ouro.mcp.sdk import SDKMCPClientFactory

    return SDKMCPClientFactory()


def _default_credential_resolver(reference: str) -> str | None:
    if reference.startswith("env:"):
        return os.environ.get(reference.removeprefix("env:"))
    if reference.startswith("inline:"):
        return reference.removeprefix("inline:")
    return reference or None
