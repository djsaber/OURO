from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ouro.application.agent_models import SpawnAgentRequest
from ouro.automation.models import TaskConfiguration, TaskSchedule
from ouro.core.provider import ProviderStreamEvent
from ouro.providers import FauxProvider
from ouro.tools import Tool, ToolDefinition, ToolResult
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation_history import ConversationHistory
from ouro.tui.screens import ChoiceScreen
from ouro.tui.widgets import ConversationView, ToolActivityRow
from tests.application.test_agent_multi_host import application, interaction
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import _wait_for_screen
from tests.tui.test_tasks import choose, streaming_application


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_failed_child_tools_stop_animating_live_and_after_reopening(tmp_path, backend):
    owner = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    viewer = application(tmp_path, backend, "C:/Windows/powershell.exe", kind="windows_powershell")
    app = OuroTuiApp(application=viewer)
    async with owner:
        parent = await owner.runtime.run("Prepare a delegation")
        child = await owner.agents.spawn_agent(
            SpawnAgentRequest(
                request_id="failed-child",
                parent_session_id=parent.session.session_id,
                parent_run_id=parent.run.run_id,
                agent_id="general",
                task="Read system information",
                description="Fail cross-host contract",
                execution_mode="sync",
            )
        )
        pending = await interaction(owner, parent.session.session_id)
        async with run_ready_app(app, size=(120, 40)) as pilot:
            await app._open_session(child.child_session_id)
            assert app._projection.activity.status == "waiting_approval"
            handle = await viewer.agents.resolve_agent_approval(
                pending.delegation_id,
                child_session_id=pending.child_session_id,
                child_run_id=pending.child_run_id,
                tool_call_id=pending.tool_call_id,
                approval_id=pending.request_id,
                approved=True,
            )
            outcome = await asyncio.wait_for(viewer.supervisor.wait(handle), 5)
            assert outcome.status == "failed"
            async with asyncio.timeout(8):
                while app._projection.activity.status != "failed":
                    await pilot.pause()
            for reopen in (False, True):
                if reopen:
                    await app._open_session(parent.session.session_id)
                    await app._open_session(child.child_session_id)
                await pilot.pause()
                tools = app._projection.activity.tool_calls
                assert tools and all(tool.status == "failed" for tool in tools)
                assert not app.query_one(ConversationView)._tools
                assert not list(app.query(ToolActivityRow))
                assert "PowerShell" in app.export_screenshot()
                assert app._projection.activity.activity == "failed"


class LiveToolProvider:
    provider_id = "faux"

    def __init__(self):
        self.turns = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.deltas = asyncio.Queue()

    async def stream(self, *, model, messages, system_prompt="", tools=None):
        self.turns += 1
        if self.turns == 1:
            yield ProviderStreamEvent(
                type="reasoning_delta", reasoning_content="Inspect the workspace."
            )
            yield ProviderStreamEvent(type="text_delta", text="Before tool.")
            yield ProviderStreamEvent.from_tool_call(
                {"id": "call_probe", "name": "Probe", "arguments": {}}
            )
            yield ProviderStreamEvent(type="done")
            return
        while True:
            event = await self.deltas.get()
            yield event
            if event.type == "done":
                return

    async def probe(self, invocation):
        self.started.set()
        await self.release.wait()
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="Inspected.",
        )

    def text(self, value):
        self.deltas.put_nowait(ProviderStreamEvent(type="text_delta", text=value))


async def visible(pilot, app, text):
    async with asyncio.timeout(8):
        while text not in app.export_screenshot():
            await pilot.pause()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_two_tuis_follow_tools_and_consecutive_runs_without_duplicate_summaries(
    tmp_path, backend
):
    provider = LiveToolProvider()
    owner = streaming_application(tmp_path, provider, backend=backend)
    viewer = streaming_application(tmp_path, FauxProvider(), backend=backend)
    owner.runtime.tools.register(
        Tool(
            definition=ToolDefinition(name="Probe", description="Inspect", effect="read_only"),
            handler=provider.probe,
        )
    )
    session = owner.runtime.sessions.create(title="Two TUI observers").session
    first, second = OuroTuiApp(application=owner), OuroTuiApp(application=viewer)
    async with (
        run_ready_app(first, size=(100, 30)) as first_pilot,
        run_ready_app(second, size=(100, 30)) as second_pilot,
    ):
        handle = await owner.supervisor.submit("Inspect", session_id=session.session_id)
        await asyncio.wait_for(provider.started.wait(), 5)
        for app in (first, second):
            await app._open_session(session.session_id)
            assert app._projection.activity.activity == "executing_tools"
        provider.release.set()
        run_ids = []
        for index, text in enumerate(("First shared answer.", "Second shared answer.")):
            if index:
                handle = await owner.supervisor.submit("Continue", session_id=session.session_id)
            provider.text(text)
            provider.deltas.put_nowait(ProviderStreamEvent(type="done"))
            result = await asyncio.wait_for(owner.supervisor.wait(handle), 5)
            run_ids.append(result.run_id)
            for app, pilot in ((first, first_pilot), (second, second_pilot)):
                async with asyncio.timeout(8):
                    while (
                        app._projection.activity.status != "completed"
                        or app._projection.activity.run_id != result.run_id
                        or sum(
                            entry.kind == "run"
                            for entry in app.query_one(ConversationHistory).entries
                        )
                        < index + 1
                    ):
                        await pilot.pause()
                await visible(pilot, app, text.split()[0])
                assert text in app._projection.assistant_text
                summaries = [
                    entry.run
                    for entry in app.query_one(ConversationHistory).entries
                    if entry.kind == "run"
                ]
                assert [summary.run_id for summary in summaries] == run_ids
                assert not app.query_one(ConversationView)._tools
        assert first._projection.last_sequence == second._projection.last_sequence
        assert not viewer.supervisor._jobs_by_session


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_task_owned_by_another_runtime_streams_through_tools_and_session_switches(
    tmp_path, backend
):
    provider = LiveToolProvider()
    owner = streaming_application(tmp_path, provider, backend=backend)
    viewer = streaming_application(tmp_path, FauxProvider(), backend=backend)
    owner.runtime.tools.register(
        Tool(
            definition=ToolDefinition(
                name="Probe", description="Inspect the workspace", effect="read_only"
            ),
            handler=provider.probe,
        )
    )
    app = OuroTuiApp(application=viewer)
    await owner.start()
    try:
        async with run_ready_app(app, size=(120, 40)) as pilot:
            task = await owner.tasks.create(
                TaskConfiguration(
                    title="Shared background task",
                    description="Inspect and report",
                    workspace=str(tmp_path),
                    model="faux/faux",
                    schedule=TaskSchedule(
                        frequency="custom",
                        unit="minutes",
                        starts_at=datetime.now(UTC) + timedelta(seconds=0.1),
                    ),
                )
            )
            async with asyncio.timeout(8):
                await provider.started.wait()
            task = await owner.tasks.get(task.task_id)
            assert app.query_one(ConversationView).showing_welcome
            await pilot.press("ctrl+t")
            await _wait_for_screen(app, ChoiceScreen, title="TASK LIST")
            await choose(pilot, app, "Shared background task")
            await choose(pilot, app, "Open task session")
            await visible(pilot, app, "Before")
            assert app._projection.session_id == task.session_id
            assert app._projection.activity.activity == "executing_tools"

            # A completed tool must transition into live text before the Run ends.
            provider.release.set()
            provider.text("Live delta. ")
            await visible(pilot, app, "Live")
            assert app._projection.activity.status == "running"
            assert app._projection.activity.activity == "streaming_text"

            other = viewer.runtime.sessions.create(title="Other session").session
            previous = app._observation_subscription
            await app._open_session(other.session_id)
            assert previous.closed
            provider.text("While away. ")
            async with asyncio.timeout(5):
                while not any(
                    event.payload.get("text") == "While away. "
                    for event in owner.runtime.store.load_session(task.session_id).events
                ):
                    await pilot.pause()
            assert "While" not in app.export_screenshot()
            await app._open_session(task.session_id)
            await visible(pilot, app, "While")
            provider.text("After return.")
            await visible(pilot, app, "After")
            assert app._projection.activity.status == "running"

            provider.deltas.put_nowait(ProviderStreamEvent(type="done"))
            async with asyncio.timeout(8):
                while app._projection.activity.status != "completed":
                    await pilot.pause()
            for text in ("Live", "While", "After"):
                assert text in app.export_screenshot()
            assert provider.turns == 2
            assert not viewer.supervisor._jobs_by_session
    finally:
        provider.release.set()
        provider.deltas.put_nowait(ProviderStreamEvent(type="done"))
        await owner.close()
