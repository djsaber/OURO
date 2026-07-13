from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.cli.approval_commands import (
    abort_run_command,
    approve_tool_command,
    reject_tool_command,
)
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.mcp import MCPConfigStore, MCPRuntimeResource, MCPServerConfig
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools

MCP_ECHO_SERVER = Path(__file__).parents[1] / "mcp" / "fixtures" / "echo_server.py"


def _runtime(tmp_path: Path, event_batches: list[list[ProviderStreamEvent]]) -> RuntimeService:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(event_batches=event_batches),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=registry,
    )


@pytest.mark.asyncio
async def test_approve_command_continues_run(monkeypatch, tmp_path, capsys) -> None:
    target = tmp_path / "approved.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "approved"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Done."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    monkeypatch.setattr("ouro.cli.approval_commands.build_default_runtime", lambda home: runtime)

    run_result = await runtime.run("write file")
    assert run_result.run.status == "waiting_approval"
    session_id = run_result.session.session_id

    code = await approve_tool_command(session_id, home=str(tmp_path))
    captured = capsys.readouterr()

    assert code == 0
    assert target.read_text(encoding="utf-8") == "approved"
    assert "Done." in captured.out


@pytest.mark.asyncio
async def test_approve_command_reconnects_mcp_tool_for_durable_resume(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    MCPConfigStore(tmp_path).upsert(
        MCPServerConfig(
            server_id="echo",
            name="Echo",
            command=sys.executable,
            args=[str(MCP_ECHO_SERVER), "--transport", "stdio"],
            auto_start=True,
            timeout_seconds=5,
        )
    )
    initial_runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "mcp_approval",
                        "name": "echo__echo",
                        "arguments": {"value": "approved MCP"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ]
        ],
    )
    async with MCPRuntimeResource.from_config(initial_runtime.store.root, initial_runtime.tools):
        waiting = await initial_runtime.run("use approved MCP")
    assert waiting.run.status == "waiting_approval"

    resumed_runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(type="text_delta", text="MCP approval resumed."),
                ProviderStreamEvent(type="done"),
            ]
        ],
    )
    monkeypatch.setattr(
        "ouro.cli.approval_commands.build_default_runtime", lambda home: resumed_runtime
    )

    code = await approve_tool_command(waiting.session.session_id, home=str(tmp_path))

    captured = capsys.readouterr()
    assert code == 0
    assert "MCP approval resumed." in captured.out
    snapshot = resumed_runtime.store.load_session(waiting.session.session_id, recover=False)
    tool_message = next(message for message in snapshot.messages if message.role == "tool_result")
    assert tool_message.tool_result is not None
    assert tool_message.tool_result.tool_name == "echo__echo"
    assert tool_message.tool_result.is_error is False
    assert resumed_runtime.tools.names() == ["AskUser", "Bash", "Edit", "Read", "Write"]


@pytest.mark.asyncio
async def test_reject_command_returns_reason_to_model(monkeypatch, tmp_path, capsys) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(tmp_path / "rejected.txt"), "content": "x"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Understood."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    monkeypatch.setattr("ouro.cli.approval_commands.build_default_runtime", lambda home: runtime)

    run_result = await runtime.run("write file")
    assert run_result.run.status == "waiting_approval"
    session_id = run_result.session.session_id

    code = await reject_tool_command(session_id, reason="unsafe", home=str(tmp_path))
    captured = capsys.readouterr()

    assert code == 0
    assert "Understood." in captured.out
    assert not (tmp_path / "rejected.txt").exists()


@pytest.mark.asyncio
async def test_approve_with_modified_arguments(monkeypatch, tmp_path, capsys) -> None:
    target = tmp_path / "modified.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(tmp_path / "original.txt"), "content": "x"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Modified and written."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    monkeypatch.setattr("ouro.cli.approval_commands.build_default_runtime", lambda home: runtime)

    run_result = await runtime.run("write file")
    assert run_result.run.status == "waiting_approval"
    session_id = run_result.session.session_id

    code = await approve_tool_command(
        session_id,
        home=str(tmp_path),
        arguments_override={"path": str(target), "content": "modified"},
    )
    captured = capsys.readouterr()

    assert code == 0
    assert target.read_text(encoding="utf-8") == "modified"
    assert "Modified and written." in captured.out


@pytest.mark.asyncio
async def test_abort_command_aborts_waiting_approval(monkeypatch, tmp_path, capsys) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(tmp_path / "aborted.txt"), "content": "x"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    monkeypatch.setattr("ouro.cli.approval_commands.build_default_runtime", lambda home: runtime)

    run_result = await runtime.run("write file")
    assert run_result.run.status == "waiting_approval"
    session_id = run_result.session.session_id

    code = await abort_run_command(session_id, reason="changed my mind", home=str(tmp_path))
    captured = capsys.readouterr()

    assert code == 0
    assert "Run aborted" in captured.out
    assert not (tmp_path / "aborted.txt").exists()


@pytest.mark.asyncio
async def test_approve_without_waiting_run_fails(monkeypatch, tmp_path, capsys) -> None:
    runtime = _runtime(
        tmp_path,
        [[ProviderStreamEvent(type="text_delta", text="ok"), ProviderStreamEvent(type="done")]],
    )
    monkeypatch.setattr("ouro.cli.approval_commands.build_default_runtime", lambda home: runtime)

    run_result = await runtime.run("hello")
    assert run_result.run.status == "completed"
    session_id = run_result.session.session_id

    code = await approve_tool_command(session_id, home=str(tmp_path))
    captured = capsys.readouterr()

    assert code == 1
    assert "no active run" in captured.err
