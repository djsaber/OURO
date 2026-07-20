from __future__ import annotations

from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.sessions import SessionService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import SessionBusyError
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools


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
        default_workspace=tmp_path,
    )


@pytest.mark.asyncio
async def test_auto_mode_executes_protected_tools_and_persists_on_session(tmp_path) -> None:
    target = tmp_path / "auto.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "automatic"},
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

    result = await runtime.run("write", approval_mode="auto")

    assert result.run.status == "completed"
    assert result.session.approval_mode == "auto"
    assert target.read_text(encoding="utf-8") == "automatic"
    persisted = runtime.store.load_session(result.session.session_id).session
    assert persisted.approval_mode == "auto"


@pytest.mark.asyncio
async def test_approval_mode_requires_approval_for_automatic_tools(tmp_path) -> None:
    target = tmp_path / "read.txt"
    target.write_text("contents", encoding="utf-8")
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Read",
                        "arguments": {"path": str(target)},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ]
        ],
    )

    result = await runtime.run("read", approval_mode="approval")

    assert result.run.status == "waiting_approval"
    assert result.run.active_tool_batch is not None
    invocation = result.run.active_tool_batch.calls[0].invocation
    assert invocation is not None
    assert invocation.permission_mode == "approval"


@pytest.mark.asyncio
async def test_deny_mode_rejects_automatic_tools_without_requesting_approval(tmp_path) -> None:
    target = tmp_path / "blocked.txt"
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "blocked"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Denied."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )

    result = await runtime.run("write", approval_mode="deny")

    assert result.run.status == "completed"
    assert result.session.approval_mode == "deny"
    assert not target.exists()
    assert result.tool_results[0].structured["error_type"] == "ToolPermissionDenied"


@pytest.mark.asyncio
async def test_existing_idle_session_can_change_mode_with_next_run(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(type="text_delta", text="First."),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Second."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    first = await runtime.run("first")

    second = await runtime.run(
        "second",
        session_id=first.session.session_id,
        approval_mode="auto",
    )

    assert second.run.status == "completed"
    assert second.session.approval_mode == "auto"


@pytest.mark.asyncio
async def test_existing_idle_session_can_enable_afk_policies_with_next_run(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(type="text_delta", text="First."),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="Second."),
                ProviderStreamEvent(type="done"),
            ],
        ],
    )
    first = await runtime.run("first")

    second = await runtime.run(
        "second",
        session_id=first.session.session_id,
        approval_mode="auto",
        interaction_mode="unattended",
    )

    assert second.run.status == "completed"
    assert second.session.approval_mode == "auto"
    assert second.session.interaction_mode == "unattended"
    persisted = runtime.store.load_session_record(second.session.session_id)
    assert persisted.interaction_mode == "unattended"


@pytest.mark.asyncio
async def test_run_rejects_mode_change_while_session_has_active_run(tmp_path) -> None:
    runtime = _runtime(
        tmp_path,
        [
            [
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
                        "id": "call_1",
                        "name": "Write",
                        "arguments": {"path": str(tmp_path / "pending.txt"), "content": "x"},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ]
        ],
    )
    waiting = await runtime.run("write")

    with pytest.raises(SessionBusyError, match="cannot change approval mode"):
        await runtime.run(
            "queued input",
            session_id=waiting.session.session_id,
            approval_mode="auto",
        )

    with pytest.raises(SessionBusyError, match="cannot change interaction mode"):
        await runtime.run(
            "queued input",
            session_id=waiting.session.session_id,
            interaction_mode="unattended",
        )


def test_branch_inherits_session_approval_mode(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    sessions = SessionService(store)
    source = sessions.create(title="Source", approval_mode="auto").session
    message = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="branch here",
        sequence=1,
    )
    store.append_message(message)

    branch = sessions.branch(source.session_id, message.message_id)

    assert branch.session.approval_mode == "auto"
    persisted = store.load_session(branch.session.session_id).session
    assert persisted.approval_mode == "auto"


def test_afk_preset_updates_both_session_policies_atomically(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    sessions = SessionService(store)
    source = sessions.create(title="AFK").session

    enabled = sessions.set_afk_mode(source.session_id, enabled=True)

    assert enabled.session.approval_mode == "auto"
    assert enabled.session.interaction_mode == "unattended"
    catalog_entry = store.read_catalog().sessions[0]
    assert catalog_entry.approval_mode == "auto"
    assert catalog_entry.interaction_mode == "unattended"

    disabled = sessions.set_afk_mode(source.session_id, enabled=False)

    assert disabled.session.approval_mode == "inherit"
    assert disabled.session.interaction_mode == "interactive"


def test_branch_inherits_session_interaction_mode(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    sessions = SessionService(store)
    source = sessions.create(
        title="Source AFK",
        approval_mode="auto",
        interaction_mode="unattended",
    ).session
    message = MessageRecord.text(
        session_id=source.session_id,
        role="user",
        text="branch here",
        sequence=1,
    )
    store.append_message(message)

    branch = sessions.branch(source.session_id, message.message_id)

    assert branch.session.approval_mode == "auto"
    assert branch.session.interaction_mode == "unattended"
    persisted = store.load_session_record(branch.session.session_id)
    assert persisted.interaction_mode == "unattended"
