from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ouro.automation.models import TaskConfiguration, TaskSchedule
from ouro.core.provider import ProviderStreamEvent
from ouro.providers import FauxProvider
from ouro.tools import Tool, ToolDefinition, ToolResult
from ouro.tui.app import OuroTuiApp
from ouro.tui.screens import ChoiceScreen
from ouro.tui.widgets import ConversationView
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import _wait_for_screen
from tests.tui.test_tasks import choose, streaming_application


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
