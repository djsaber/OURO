"""TUI application scenarios: tool history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.core.messages import MessageRecord, TextContent, ToolExecutionTiming, ToolResultReference
from ouro.core.tools import ToolCallRequest
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.transcript import _tool_activities_from_messages
from ouro.tui.conversation.view import ConversationView
from tests.tui.app.support import _application
from tests.tui.helpers import run_ready_app, wait_for_history_render


@pytest.mark.asyncio
async def test_completed_tool_batch_archives_in_source_order(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    first = RuntimeToolCallActivity(
        tool_call_id="call_first",
        tool_name="First",
        status="running",
        source_index=0,
    )
    second = RuntimeToolCallActivity(
        tool_call_id="call_second",
        tool_name="Second",
        status="running",
        source_index=1,
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.add_user("Run both tools")
        await conversation.append_assistant("Preparing both tools.")
        await pilot.pause()
        history = conversation.query_one(ConversationHistory)

        for first_status in ("running", "completed"):
            await conversation.sync_tools(
                (first.model_copy(update={"status": first_status}), second)
            )
            await pilot.pause()

            assert not [entry for entry in history.entries if entry.kind == "tool"]
            assert len(conversation.query(".tool-row")) == 2
            assert history.region.height == len(history.lines)
            rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            assert rendered.index("Preparing both tools.") < rendered.index("First")
            assert rendered.index("First") < rendered.index("Second")

        await conversation.sync_tools(
            (
                first.model_copy(update={"status": "completed"}),
                second.model_copy(update={"status": "completed"}),
            )
        )
        await pilot.pause()
        tool_ids = [
            entry.tool.tool_call_id
            for entry in history.entries
            if entry.kind == "tool" and entry.tool is not None
        ]
        assert tool_ids == ["call_first", "call_second"]
        assert not conversation.query(".tool-row")
        assert history.region.height == len(history.lines)
        rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert rendered.index("Preparing both tools.") < rendered.index("First")
        assert rendered.index("First") < rendered.index("Second")


@pytest.mark.asyncio
async def test_tool_batch_mounts_publishes_history_and_unmounts_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    tools = tuple(
        RuntimeToolCallActivity(
            tool_call_id=f"call_{index}",
            tool_name=f"Tool{index}",
            status="running",
            source_index=index,
        )
        for index in range(4)
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.add_user("Run one tool batch")
        await pilot.pause()
        mounted_batches: list[tuple[str, ...]] = []
        mount_all = conversation._transcript.mount_all

        async def record_mount_all(widgets, **kwargs):
            rows = tuple(widgets)
            mounted_batches.append(tuple(row.tool_call_id for row in rows))
            await mount_all(rows, **kwargs)

        monkeypatch.setattr(conversation._transcript, "mount_all", record_mount_all)
        await conversation.sync_tools(tools)

        history = conversation.query_one(ConversationHistory)
        size_publications = 0
        sync_virtual_size = history._sync_virtual_size

        def record_size_publication() -> None:
            nonlocal size_publications
            size_publications += 1
            sync_virtual_size()

        removed_batches: list[tuple[str, ...]] = []
        remove_children = conversation._transcript.remove_children

        async def record_remove_children(widgets):
            rows = tuple(widgets)
            removed_batches.append(tuple(row.tool_call_id for row in rows))
            await remove_children(rows)

        monkeypatch.setattr(history, "_sync_virtual_size", record_size_publication)
        monkeypatch.setattr(conversation._transcript, "remove_children", record_remove_children)
        await conversation.sync_tools(
            tuple(tool.model_copy(update={"status": "completed"}) for tool in tools)
        )

        expected_ids = tuple(tool.tool_call_id for tool in tools)
        assert mounted_batches == [expected_ids]
        assert size_publications == 1
        assert removed_batches == [expected_ids]
        assert not conversation.query(".tool-row")


@pytest.mark.asyncio
async def test_history_load_renders_tool_calls_between_messages(tmp_path: Path) -> None:
    """Loading a persisted session must render tool calls, not just text.

    A tool call spans two messages: an assistant message carrying the request
    (``tool_calls``) and a following ``tool_result`` message carrying the
    outcome reference. ``load_messages`` must pair them and render a
    ``ToolActivityRow`` in the right dialogue position.
    """
    app = OuroTuiApp(application=_application(tmp_path))
    tool_call_id = "tool_history_read"
    tool_started_at = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    messages = (
        MessageRecord(
            session_id="ses_tools",
            role="user",
            content=[TextContent(text="Read the config")],
        ),
        MessageRecord(
            session_id="ses_tools",
            role="assistant",
            content=[],
            tool_calls=[
                ToolCallRequest(
                    id=tool_call_id,
                    name="read",
                    arguments={"path": "config.json"},
                )
            ],
        ),
        MessageRecord(
            session_id="ses_tools",
            role="tool_result",
            tool_result=ToolResultReference(
                tool_call_id=tool_call_id,
                tool_name="read",
                is_error=False,
            ),
            tool_execution=ToolExecutionTiming(
                started_at=tool_started_at,
                ended_at=tool_started_at + timedelta(seconds=3),
            ),
        ),
        MessageRecord(
            session_id="ses_tools",
            role="assistant",
            content=[TextContent(text="The config has 3 lines.")],
        ),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await wait_for_history_render(pilot)

        history = conversation.query_one(ConversationHistory)
        tools = [entry.tool for entry in history.entries if entry.tool is not None]
        assert len(tools) == 1
        assert tools[0].tool_call_id == tool_call_id
        assert tools[0].status == "completed"
        assert tools[0].started_at == tool_started_at
        assert tools[0].ended_at == tool_started_at + timedelta(seconds=3)

        rendered = history.rendered_text
        assert "read" in rendered
        assert "config.json" in rendered
        assert "3.0s" in rendered

        # Dialogue order: user text, tool row, assistant text — interleaved.
        kinds = [entry.kind for entry in history.entries]
        assert kinds == ["user", "tool", "assistant"]


@pytest.mark.asyncio
async def test_history_load_keeps_the_active_tool_batch_in_live_rows(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    run_id = "run_active_tools"
    tool_call_id = "spawn_active"
    request = MessageRecord(
        session_id="ses_active_tools",
        run_id=run_id,
        role="assistant",
        content=[],
        tool_calls=[
            ToolCallRequest(
                id=tool_call_id,
                name="SpawnAgent",
                arguments={"task": "Inspect the active child."},
            )
        ],
    )
    active = RuntimeToolCallActivity(
        tool_call_id=tool_call_id,
        tool_name="SpawnAgent",
        status="waiting_child",
        source_index=0,
        arguments={"task": "Inspect the active child."},
        started_at=datetime.now(UTC) - timedelta(seconds=2),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(
            (request,),
            mutable_tool_calls=((run_id, tool_call_id),),
        )
        conversation.begin_model_request(run_id=run_id)
        await conversation.sync_tools((active,))
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert not [entry for entry in history.entries if entry.tool is not None]
        row = conversation._tools[tool_call_id]
        assert row.is_attached is True
        assert row.content_region.x == conversation.content_region.x == app.screen.region.x + 1
        assert (
            row.content_region.right
            == conversation.content_region.right
            == app.screen.region.right - 1
        )
        assert row._timer is not None
        frame = row._frame
        row._tick()
        assert row._frame != frame


def test_history_does_not_infer_tool_execution_timing_from_message_boundaries() -> None:
    started_at = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    call_id = "untimed_call"
    request = MessageRecord(
        session_id="ses_untimed_tools",
        role="assistant",
        created_at=started_at,
        tool_calls=[ToolCallRequest(id=call_id, name="read")],
    )
    result = MessageRecord(
        session_id="ses_untimed_tools",
        role="tool_result",
        created_at=started_at + timedelta(seconds=4),
        tool_result=ToolResultReference(tool_call_id=call_id, tool_name="read"),
    )

    activity = _tool_activities_from_messages((request, result))[call_id]

    assert activity.started_at is None
    assert activity.ended_at is None


@pytest.mark.asyncio
async def test_history_load_projects_scaffold_visibility_from_message_record(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    messages = (
        MessageRecord.text(
            session_id="ses_scaffold",
            role="user",
            text="internal goal instruction",
            scaffold=True,
            semantic_tag="goal-create",
            sequence=1,
        ),
        MessageRecord.text(
            session_id="ses_scaffold",
            role="user",
            text="Ship the visible objective",
            scaffold=False,
            sequence=2,
        ),
        MessageRecord.text(
            session_id="ses_scaffold",
            role="assistant",
            text="internal assistant scaffold",
            scaffold=True,
            sequence=3,
        ),
        MessageRecord.text(
            session_id="ses_scaffold",
            role="assistant",
            text="Visible answer",
            scaffold=False,
            sequence=4,
        ),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)

        entries = conversation.query_one(ConversationHistory).entries
        assert [(entry.kind, entry.text) for entry in entries] == [
            ("user", "Ship the visible objective"),
            ("assistant", "Visible answer"),
        ]


@pytest.mark.asyncio
async def test_history_load_marks_failed_tool_calls(tmp_path: Path) -> None:
    """A tool_result with is_error must surface as a failed tool row."""
    app = OuroTuiApp(application=_application(tmp_path))
    tool_call_id = "tool_history_fail"
    messages = (
        MessageRecord(
            session_id="ses_fail",
            role="assistant",
            content=[],
            tool_calls=[
                ToolCallRequest(
                    id=tool_call_id,
                    name="write",
                    arguments={"path": "/readonly/x"},
                )
            ],
        ),
        MessageRecord(
            session_id="ses_fail",
            role="tool_result",
            tool_result=ToolResultReference(
                tool_call_id=tool_call_id,
                tool_name="write",
                is_error=True,
            ),
        ),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(messages)
        await wait_for_history_render(pilot)

        history = conversation.query_one(ConversationHistory)
        tools = [entry.tool for entry in history.entries if entry.tool is not None]
        assert len(tools) == 1
        assert tools[0].status == "failed"
        assert "failed" not in history.rendered_text
