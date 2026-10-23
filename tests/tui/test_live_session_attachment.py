from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ouro.application.agent_models import SpawnAgentRequest
from ouro.core.provider import ProviderStreamEvent
from ouro.core.tasks import AgentTaskTarget, TaskConfiguration, TaskSchedule
from ouro.providers import FauxProvider
from ouro.tools import Tool, ToolDefinition, ToolResult
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation_history import ConversationHistory
from ouro.tui.screens import ChoiceScreen
from ouro.tui.theme import theme_for
from ouro.tui.widgets import AssistantTurn, ConversationView, ToolActivityRow
from tests.application.helpers import wait_for_session_state
from tests.application.test_agent_multi_host import application, interaction
from tests.tui.helpers import release_on_exit, run_ready_app, wait_for_events, wait_until
from tests.tui.test_app import _wait_for_screen, _wait_for_worker_group
from tests.tui.test_tasks import choose, streaming_application


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_failed_child_tools_remain_failed_live_and_after_reopening(tmp_path, backend):
    owner = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    viewer = application(
        tmp_path, backend, "C:/Windows/powershell.exe", kind="windows_powershell", fail_child=True
    )
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
                description="Record a tool failure before the child model fails",
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
            outcome = await asyncio.wait_for(viewer.supervisor.wait(handle), 60)
            assert outcome.status == "failed"
            await wait_until(
                lambda: app._projection.activity.status == "failed",
                pilot,
                description="failed child observed",
            )
            for reopen in (False, True):
                if reopen:
                    await app._open_session(parent.session.session_id)
                    await app._open_session(child.child_session_id)
                await pilot.pause()
                tools = [
                    entry.tool
                    for entry in app.query_one(ConversationHistory).entries
                    if entry.tool is not None
                ]
                assert tools and all(tool.status == "failed" for tool in tools)
                assert app._projection.activity.tool_calls == ()
                assert not app.query_one(ConversationView)._tools
                assert not list(app.query(ToolActivityRow))
                assert "PowerShell" in visible_text(app)
                assert app._projection.activity.activity == "failed"


class LiveToolProvider:
    provider_id = "faux"

    def __init__(self):
        self.turns = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.deltas = asyncio.Queue()

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
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

    async def probe(self, invocation, context):
        self.started.set()
        await self.release.wait()
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="Inspected.",
        )

    def text(self, value):
        self.deltas.put_nowait(ProviderStreamEvent(type="text_delta", text=value))


def visible_text(app):
    return app.query_one(ConversationHistory).rendered_text + app.query_one(AssistantTurn).source


async def visible(pilot, app, text):
    await wait_until(
        lambda: text in visible_text(app), pilot, description=f"attached transcript: {text}"
    )


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
    async with owner, release_on_exit(provider.release):
        # Prepare the executing tool before mounting its two observers. This test
        # exercises attachment and live updates, not startup under UI render load.
        handle = await owner.supervisor.submit("Inspect", session_id=session.session_id)
        await asyncio.wait_for(provider.started.wait(), 60)
        async with (
            run_ready_app(first, size=(100, 30)) as first_pilot,
            run_ready_app(second, size=(100, 30)) as second_pilot,
            release_on_exit(provider.release),
        ):
            for app in (first, second):
                await app._open_session(session.session_id)
                assert app._projection.activity.activity == "executing_tools"
            provider.release.set()
            run_ids = []
            for index, text in enumerate(("First shared answer.", "Second shared answer.")):
                if index:
                    handle = await owner.supervisor.submit(
                        "Continue", session_id=session.session_id
                    )
                provider.text(text)
                provider.deltas.put_nowait(ProviderStreamEvent(type="done"))
                result = await asyncio.wait_for(owner.supervisor.wait(handle), 60)
                run_ids.append(result.run_id)
                for app, pilot in ((first, first_pilot), (second, second_pilot)):
                    await wait_until(
                        lambda app=app, result=result, index=index: (
                            app._projection.activity.status == "completed"
                            and app._projection.activity.run_id == result.run_id
                            and sum(
                                entry.kind == "run"
                                for entry in app.query_one(ConversationHistory).entries
                            )
                            == index + 1
                        ),
                        pilot,
                        description="completed run and its summary observed",
                    )
                    await visible(pilot, app, text)
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
    tmp_path, backend, monkeypatch
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
    # Creation and dispatch share a logical instant, independent of validation I/O.
    due = datetime.now(UTC)
    monkeypatch.setattr(owner.tasks, "clock", lambda: due)
    await owner.start()
    try:
        async with (
            run_ready_app(app, size=(120, 40)) as pilot,
            release_on_exit(provider.release),
        ):
            task = await owner.tasks.create(
                TaskConfiguration(
                    title="Shared background task",
                    schedule=TaskSchedule(
                        frequency="custom",
                        unit="minutes",
                        starts_at=due,
                    ),
                    target=AgentTaskTarget(
                        prompt="Inspect and report", workspace=str(tmp_path), model="faux/faux"
                    ),
                )
            )
            await wait_for_events(
                provider.started, pilot=pilot, description="background tool execution"
            )
            task = await owner.tasks.get(task.task_id)
            assert app.query_one(ConversationView).showing_welcome
            await pilot.press("ctrl+t")
            await _wait_for_screen(app, ChoiceScreen, title="Tasks")
            await choose(pilot, app, "Shared background task")
            await _wait_for_screen(
                app,
                ChoiceScreen,
                title=theme_for(app).section_separator.join(("TASK", "Shared background task")),
            )
            await choose(pilot, app, "History")
            await _wait_for_screen(
                app,
                ChoiceScreen,
                title=theme_for(app).section_separator.join(("TASK EXECUTIONS", "LATEST 50")),
            )
            selection_group = f"choice-selection-{id(app.screen)}"
            await pilot.press("enter")
            await _wait_for_worker_group(app, pilot, group=selection_group)
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
            await wait_for_session_state(
                owner.supervisor,
                task.session_id,
                lambda: any(
                    event.payload.get("text") == "While away. "
                    for event in owner.runtime.store.load_session(task.session_id).events
                ),
                event_types=frozenset({"model_text_delta"}),
            )
            assert "While" not in visible_text(app)
            await app._open_session(task.session_id)
            await visible(pilot, app, "While")
            provider.text("After return.")
            await visible(pilot, app, "After")
            assert app._projection.activity.status == "running"

            provider.deltas.put_nowait(ProviderStreamEvent(type="done"))
            await wait_until(
                lambda: app._projection.activity.status == "completed",
                pilot,
                description="background task completion observed",
            )
            for text in ("Live", "While", "After"):
                await visible(pilot, app, text)
            assert provider.turns == 2
            assert not viewer.supervisor._jobs_by_session
    finally:
        provider.release.set()
        provider.deltas.put_nowait(ProviderStreamEvent(type="done"))
        await owner.close()
