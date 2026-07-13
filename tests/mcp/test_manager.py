from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from ouro.core.cancellation import AbortSignal
from ouro.mcp import (
    MCPManager,
    MCPRemoteTool,
    MCPRemoteToolResult,
    MCPServerCapabilities,
    MCPServerConfig,
    MCPServerInfo,
    MCPStateError,
    MCPToolCallError,
)
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionAborted,
    ToolExecutionContext,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)


@dataclass
class FakeSession:
    tools: list[MCPRemoteTool] = field(default_factory=list)
    result: MCPRemoteToolResult = field(default_factory=lambda: MCPRemoteToolResult(content="ok"))
    initialize_error: Exception | None = None
    call_error: Exception | None = None
    call_delay: float = 0
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    ping_count: int = 0

    async def initialize(self) -> MCPServerInfo:
        if self.initialize_error is not None:
            raise self.initialize_error
        return MCPServerInfo(
            name="Fake MCP",
            version="1.0",
            protocol_version="2025-11-25",
            capabilities=MCPServerCapabilities(tools=True, tools_list_changed=True),
        )

    async def list_tools(self, cursor: str | None = None) -> tuple[list[MCPRemoteTool], str | None]:
        if len(self.tools) > 1 and cursor is None:
            return self.tools[:1], "next"
        if cursor == "next":
            return self.tools[1:], None
        return self.tools, None

    async def call_tool(self, name: str, arguments: dict[str, object]) -> MCPRemoteToolResult:
        self.calls.append((name, arguments))
        if self.call_delay:
            await asyncio.sleep(self.call_delay)
        if self.call_error is not None:
            raise self.call_error
        return self.result

    async def ping(self) -> None:
        self.ping_count += 1


class FakeFactory:
    def __init__(self, *sessions: FakeSession) -> None:
        self.sessions = list(sessions)
        self.open_count = 0
        self.close_count = 0
        self.tools_changed = lambda: None

    @asynccontextmanager
    async def open(self, config, **callbacks) -> AsyncIterator[FakeSession]:
        del config
        self.open_count += 1
        self.tools_changed = callbacks["tools_changed"]
        session = self.sessions[min(self.open_count - 1, len(self.sessions) - 1)]
        try:
            yield session
        finally:
            self.close_count += 1


def _config(**changes) -> MCPServerConfig:
    values = {
        "server_id": "fs",
        "name": "Filesystem",
        "command": "unused-in-fake",
        "reconnect_backoff_seconds": 0.01,
        "health_check_interval_seconds": 60,
    }
    values.update(changes)
    return MCPServerConfig(**values)


def _remote_tool(name: str = "read") -> MCPRemoteTool:
    return MCPRemoteTool(
        name=name,
        title="Read",
        description="Read through MCP.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        output_schema={"type": "object"},
        annotations={"readOnlyHint": True},
    )


@pytest.mark.asyncio
async def test_manager_owns_connection_discovery_invocation_and_cleanup() -> None:
    session = FakeSession(
        tools=[_remote_tool(), _remote_tool("stat")],
        result=MCPRemoteToolResult(
            content="done",
            structured={"size": 3},
            metadata={"blocks": 1},
        ),
    )
    factory = FakeFactory(session)
    registry = ToolRegistry()
    manager = MCPManager(registry=registry, client_factory=factory)
    manager.configure(_config())

    connected = await manager.start("fs")

    assert connected.status == "connected"
    assert connected.server_info is not None
    assert connected.server_info.capabilities.tools_list_changed is True
    assert connected.tool_names == ("fs__read", "fs__stat")
    definition = registry.get("fs__read").definition
    assert definition.source == "mcp"
    assert definition.permission_mode == "approval"
    assert definition.metadata["mcp_server_id"] == "fs"
    assert definition.metadata["mcp_tool_name"] == "read"

    result = await registry.get("fs__read").execute(
        ToolInvocation(
            session_id="session",
            run_id="run",
            tool_name="fs__read",
            arguments={"path": "README.md"},
        )
    )

    assert result.content == "done"
    assert result.structured == {"size": 3}
    assert session.calls == [("read", {"path": "README.md"})]

    stopped = await manager.stop("fs")
    assert stopped.status == "stopped"
    assert registry.names() == []
    assert factory.close_count == 1


@pytest.mark.asyncio
async def test_manager_start_is_idempotent_and_configure_rejects_active_server() -> None:
    factory = FakeFactory(FakeSession())
    manager = MCPManager(client_factory=factory)
    manager.configure(_config())

    first = await manager.start("fs")
    second = await manager.start("fs")

    assert first.status == second.status == "connected"
    assert factory.open_count == 1
    with pytest.raises(MCPStateError, match="stop it first"):
        manager.configure(_config(name="Changed"))
    await manager.close()


@pytest.mark.asyncio
async def test_manager_reconnects_with_bounded_backoff() -> None:
    factory = FakeFactory(
        FakeSession(initialize_error=ConnectionError("not ready")),
        FakeSession(tools=[_remote_tool()]),
    )
    manager = MCPManager(client_factory=factory)
    manager.configure(_config(max_reconnect_attempts=1))

    record = await manager.start("fs")

    assert record.status == "connected"
    assert record.reconnect_count == 1
    assert factory.open_count == 2
    assert manager.diagnostics("fs")[0].level == "error"
    await manager.close()


@pytest.mark.asyncio
async def test_remote_disconnect_recycles_session_and_restores_tools() -> None:
    failing = FakeSession(tools=[_remote_tool()], call_error=ConnectionError("connection closed"))
    recovered = FakeSession(tools=[_remote_tool()])
    factory = FakeFactory(failing, recovered)
    registry = ToolRegistry()
    manager = MCPManager(registry=registry, client_factory=factory)
    manager.configure(_config())
    await manager.start("fs")
    invocation = ToolInvocation(
        session_id="session",
        run_id="run",
        tool_name="fs__read",
        arguments={"path": "README.md"},
    )

    with pytest.raises(MCPToolCallError, match="connection closed"):
        await registry.get("fs__read").execute(invocation)
    for _ in range(100):
        if factory.open_count == 2 and manager.get("fs").status == "connected":
            break
        await asyncio.sleep(0.01)

    assert factory.open_count == 2
    assert registry.names() == ["fs__read"]
    result = await registry.get("fs__read").execute(invocation)
    assert result.content == "ok"
    await manager.close()


@pytest.mark.asyncio
async def test_failures_and_diagnostics_redact_resolved_credentials() -> None:
    secret = "top-secret-token"
    manager = MCPManager(
        client_factory=FakeFactory(
            FakeSession(initialize_error=ConnectionError(f"rejected {secret}"))
        )
    )
    manager.configure(
        _config(
            auto_reconnect=False,
            credential_refs={"TOKEN": f"inline:{secret}"},
        )
    )

    record = await manager.start("fs")

    assert secret not in (record.error or "")
    assert "***" in (record.error or "")
    assert all(secret not in item.message for item in manager.diagnostics("fs"))


@pytest.mark.asyncio
async def test_tool_list_change_notification_atomically_refreshes_registry() -> None:
    session = FakeSession(tools=[_remote_tool()])
    factory = FakeFactory(session)
    registry = ToolRegistry()
    manager = MCPManager(registry=registry, client_factory=factory)
    manager.configure(_config())
    await manager.start("fs")

    session.tools = [_remote_tool("write")]
    factory.tools_changed()
    for _ in range(100):
        if registry.names() == ["fs__write"]:
            break
        await asyncio.sleep(0.01)

    assert registry.names() == ["fs__write"]
    assert manager.get("fs").tool_names == ("fs__write",)
    await manager.close()


@pytest.mark.asyncio
async def test_remote_tool_call_cooperates_with_abort_signal() -> None:
    session = FakeSession(tools=[_remote_tool()], call_delay=30)
    registry = ToolRegistry()
    manager = MCPManager(registry=registry, client_factory=FakeFactory(session))
    manager.configure(_config())
    await manager.start("fs")
    signal = AbortSignal()
    invocation = ToolInvocation(
        session_id="session",
        run_id="run",
        tool_name="fs__read",
        arguments={"path": "README.md"},
    )
    execution = asyncio.create_task(
        registry.get("fs__read").execute(
            invocation,
            context=ToolExecutionContext(signal=signal, poll_interval=0.001),
        )
    )
    await asyncio.sleep(0.01)
    signal.abort("cancelled by test")

    with pytest.raises(ToolExecutionAborted, match="cancelled by test"):
        await execution
    await manager.close()


@pytest.mark.asyncio
async def test_invalid_discovered_schema_fails_without_partial_registration() -> None:
    invalid = _remote_tool().model_copy(update={"input_schema": {"type": "unknown"}})
    registry = ToolRegistry()
    manager = MCPManager(
        registry=registry,
        client_factory=FakeFactory(FakeSession(tools=[invalid])),
    )
    manager.configure(_config(auto_reconnect=False))

    record = await manager.start("fs")

    assert record.status == "failed"
    assert "invalid parameters_schema" in (record.error or "")
    assert registry.names() == []


def test_explicit_adapter_registration_preserves_legacy_custom_clients() -> None:
    manager = MCPManager()
    manager.configure(_config())
    registry = ToolRegistry()

    def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="ok",
        )

    registered = manager.register_tools(
        server_id="fs",
        registry=registry,
        tools=[
            Tool(
                definition=ToolDefinition(name="mcp_read", description="Read through MCP."),
                handler=handler,
            )
        ],
    )

    assert registered == ["mcp_read"]
    assert registry.get("mcp_read").definition.metadata["mcp_server_id"] == "fs"
