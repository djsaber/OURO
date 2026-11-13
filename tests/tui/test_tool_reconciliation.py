from __future__ import annotations

import asyncio

import pytest

from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.provider import ProviderStreamEvent
from ouro.core.tools import ToolCallRequest
from ouro.providers import FauxProvider
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.view import ConversationView
from tests.tui.helpers import release_on_exit, run_ready_app, wait_for_history_render, wait_until
from tests.tui.test_tasks import streaming_application


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_tool_preparation_failure_reconciles_live_layout(tmp_path, monkeypatch, backend):
    provider = FauxProvider()
    release = asyncio.Event()

    async def stream(**kwargs):
        yield ProviderStreamEvent.from_tool_call(ToolCallRequest(id="call_read", name="Read"))
        await release.wait()
        yield ProviderStreamEvent(type="done")

    monkeypatch.setattr(provider, "stream", stream)
    application = streaming_application(tmp_path, provider, backend=backend)
    app = OuroTuiApp(application=application)

    def fail_preparation(**kwargs):
        raise ValueError("Tool preparation failed")

    monkeypatch.setattr(application.runtime.run_lifecycle, "save_batch_state", fail_preparation)
    async with run_ready_app(app, size=(100, 30)) as pilot, release_on_exit(release):
        app._submit_run("Read the workspace", "follow_up")
        conversation = app.query_one(ConversationView)
        await wait_until(
            lambda: bool(conversation.query(".tool-row")),
            pilot,
            description="live tool preparation",
        )
        rows = tuple(conversation.query(".tool-row"))
        history = app.query_one(ConversationHistory)
        release.set()
        for worker in tuple(app.workers):
            if worker.group == "run":
                await worker.wait()
        await wait_until(
            lambda: any(entry.run and entry.run.status == "failed" for entry in history.entries),
            pilot,
            description="failed run summary",
        )
        await wait_for_history_render(pilot)
        assert not conversation.query(".tool-row")
        assert not conversation._tools
        assert all(row._timer is None for row in rows)
        live = history.entries
        live_text = history.rendered_text
        await app._open_session(app._controller.session_id)
        await wait_for_history_render(pilot)
        assert history.entries == live
        assert history.rendered_text == live_text


async def test_tool_snapshot_retires_missing_rows_without_rebuilding_history(tmp_path, monkeypatch):
    app = OuroTuiApp(application=streaming_application(tmp_path, FauxProvider()))
    async with run_ready_app(app) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.begin_model_request(run_id="run")
        await conversation.add_user("Inspect two tools")
        await wait_for_history_render(pilot)
        history = app.query_one(ConversationHistory)
        entries = history.entries
        previews = tuple(
            RuntimeToolCallActivity(
                tool_call_id=f"stream:turn:{index}",
                tool_name=f"Tool{index}",
                source_index=index,
                status="streaming",
            )
            for index in range(2)
        )
        await conversation.sync_tools(previews)
        first, second = conversation.query(".tool-row")

        def unexpected_render(*args, **kwargs):
            raise AssertionError("preview removal must not render history")

        monkeypatch.setattr(history, "_schedule_buffer_render", unexpected_render)
        await conversation.sync_tools(previews[:1])
        assert tuple(conversation.query(".tool-row")) == (first,)
        assert second._timer is None
        await conversation.sync_tools(())
        await conversation.sync_tools(())
        assert not conversation.query(".tool-row")
        assert first._timer is None
        assert history.entries == entries


async def test_tool_results_update_one_call_and_preserve_the_live_batch(tmp_path):
    app = OuroTuiApp(application=streaming_application(tmp_path, FauxProvider()))
    async with run_ready_app(app):
        conversation = app.query_one(ConversationView)
        conversation.begin_model_request(run_id="run")
        calls = tuple(
            RuntimeToolCallActivity(
                tool_call_id=f"call_{index}",
                tool_name=f"Tool{index}",
                status="running",
                source_index=index,
            )
            for index in range(2)
        )
        await conversation.sync_tools(calls)
        rows = tuple(conversation.query(".tool-row"))
        history = app.query_one(ConversationHistory)

        def result(index):
            return MessageRecord(
                session_id="session",
                run_id="run",
                role="tool_result",
                tool_result=ToolResultReference(
                    tool_call_id=f"call_{index}",
                    tool_name=f"Tool{index}",
                ),
            )

        await conversation.apply_tool_result(result(1))
        assert tuple(conversation.query(".tool-row")) == rows
        assert [row.tool.status for row in rows] == ["running", "completed"]
        assert not history.entries
        await conversation.apply_tool_result(result(0))
        assert not conversation.query(".tool-row")
        assert [entry.tool.tool_call_id for entry in history.entries] == ["call_0", "call_1"]

        # A result for an archived call does not replace the current batch.
        conversation.begin_model_request(run_id="next_run")
        await conversation.sync_tools((calls[0],))
        row = conversation.query_one(".tool-row")
        await conversation.apply_tool_result(result(0))
        assert conversation.query_one(".tool-row") is row
        assert row.run_id == "next_run" and row.tool.status == "running"


@pytest.mark.parametrize("change_scope", ["run", "batch"])
async def test_tool_snapshot_reuses_call_ids_only_within_their_scope(tmp_path, change_scope):
    app = OuroTuiApp(application=streaming_application(tmp_path, FauxProvider()))
    async with run_ready_app(app):
        conversation = app.query_one(ConversationView)
        conversation.begin_model_request(run_id="run")
        call = RuntimeToolCallActivity(
            tool_call_id="reused",
            tool_name="Read",
            batch_id="batch",
            status="planned",
            source_index=0,
        )
        await conversation.sync_tools((call,))
        old_row = conversation.query_one(".tool-row")
        if change_scope == "run":
            conversation.begin_model_request(run_id="next_run")
        else:
            call = call.model_copy(update={"batch_id": "next_batch"})
        await conversation.sync_tools((call,))
        new_row = conversation.query_one(".tool-row")
        assert new_row is not old_row
        assert old_row._timer is None
        history = app.query_one(ConversationHistory)
        assert len(history.entries) == 1
        assert history.entries[0].run_id == "run"
        assert history.entries[0].tool.batch_id == "batch"
        assert history.entries[0].tool.status == "unknown_outcome"
