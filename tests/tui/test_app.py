from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import ModelMessage
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
)
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolInputRequest, ToolRegistry, builtin_tools
from ouro.tui.app import OuroTuiApp
from ouro.tui.screens import HelpScreen
from ouro.tui.widgets import AskUserPanel, AssistantTurn, CommandPopup, Composer


class BlockingStreamProvider:
    provider_id = "blocking"

    def __init__(self) -> None:
        self.first_chunk_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="text_delta", text="first ")
        self.first_chunk_sent.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="text_delta", text="second")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class ReasoningBurstThenBlockProvider:
    provider_id = "reasoning-burst"

    def __init__(self, *, reasoning_chunks: int = 270) -> None:
        self.reasoning_chunks = reasoning_chunks
        self.visible_chunk_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        for _ in range(self.reasoning_chunks):
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="r")
        yield ProviderStreamEvent(type="text_delta", text="visible while running")
        self.visible_chunk_sent.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


def _application(tmp_path: Path, text: str = "**Hello** from OURO") -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(type="text_delta", text=text),
                        ProviderStreamEvent(type="done"),
                    ]
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _approval_application(tmp_path: Path, target: Path) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_write",
                                "name": "Write",
                                "arguments": {"path": str(target), "content": "written"},
                            }
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Done."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=tools,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _blocking_application(
    tmp_path: Path,
    provider: BlockingStreamProvider | ReasoningBurstThenBlockProvider,
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="blocking", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


def _question_application(
    tmp_path: Path,
    *,
    tool_name: str = "AskUser",
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    if tool_name == "AskUser":
        for tool in builtin_tools():
            tools.register(tool)
        arguments = {
            "questions": [
                {
                    "id": "file",
                    "header": "File",
                    "question": "Which file should OURO use?",
                    "options": [
                        {
                            "label": "notes.md (Recommended)",
                            "description": "Use the notes file.",
                        },
                        {
                            "label": "README.md",
                            "description": "Use the readme file.",
                        },
                    ],
                }
            ]
        }
    else:
        tools.register(
            Tool(
                definition=ToolDefinition(
                    name=tool_name,
                    description="Ask a plugin-defined question.",
                    kind="user_input",
                    effect="read_only",
                    permission_mode="auto",
                ),
                handler=lambda _invocation: ToolInputRequest(
                    data={
                        "question": "How should the plugin continue?",
                        "options": [
                            {
                                "label": "Safely",
                                "description": "Use guarded defaults.",
                            },
                            {
                                "label": "Quickly",
                                "description": "Prefer lower latency.",
                            },
                        ],
                    }
                ),
            )
        )
        arguments = {}
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "question_1",
                                "name": tool_name,
                                "arguments": arguments,
                            }
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Answer received."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=tools,
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


@pytest.mark.asyncio
async def test_app_streams_first_run_without_waiting_for_run_result(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        composer = app.query_one(Composer)
        assert composer.disabled is False

        await pilot.click("#composer")
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.assistant_text == "**Hello** from OURO"
        assert app._projection.activity.status == "completed"
        assert app._controller.session_id is not None


@pytest.mark.asyncio
async def test_first_markdown_chunk_renders_while_provider_is_still_running(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("s", "t", "r", "e", "a", "m", "enter")
        try:
            await asyncio.wait_for(provider.first_chunk_sent.wait(), timeout=2)
            await pilot.pause(0.2)

            assert app._projection is not None
            assert app._projection.assistant_text == "first "
            assert app._projection.activity.status != "completed"
            assert app.query_one(AssistantTurn).markdown.source == "first "
        finally:
            provider.release.set()
        await pilot.pause(0.5)

        assert app._projection is not None
        assert app._projection.assistant_text == "first second"
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_reasoning_burst_cannot_starve_live_text_rendering(tmp_path: Path) -> None:
    provider = ReasoningBurstThenBlockProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)
    projection_text = ""
    activity_status = ""
    markdown_source = ""
    sink_failed = True

    async def exercise(pilot) -> None:
        nonlocal projection_text, activity_status, markdown_source, sink_failed
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("b", "u", "r", "s", "t", "enter")
        try:
            await asyncio.wait_for(provider.visible_chunk_sent.wait(), timeout=15)
            await pilot.pause(0.5)

            assert app._projection is not None
            projection_text = app._projection.assistant_text
            activity_status = app._projection.activity.status
            turns = app.query(AssistantTurn)
            markdown_source = turns.last().markdown.source if turns else ""
            events = application.runtime.sessions.event_page(
                app._projection.session_id,
                cursor=0,
                limit=1000,
            ).events
            sink_failed = any(event.event_type == "event_sink_failed" for event in events)
        finally:
            provider.release.set()
            await pilot.pause(1)
        app.exit()

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    try:
        await asyncio.wait_for(
            app.run_async(
                headless=True,
                size=(100, 32),
                auto_pilot=exercise,
            ),
            timeout=20,
        )
    finally:
        loop.set_task_factory(previous_factory)

    assert projection_text == "visible while running"
    assert activity_status != "completed"
    assert markdown_source == "visible while running"
    assert sink_failed is False


@pytest.mark.asyncio
async def test_run_result_replays_events_when_live_sink_is_delayed(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path, "Recovered response"))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        app._controller._publish_event = lambda _event: None
        await pilot.click("#composer")
        await pilot.press("h", "e", "l", "l", "o", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.assistant_text == "Recovered response"
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_composer_uses_shift_enter_for_newline(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("a", "shift+enter", "b")

        assert app.query_one(Composer).text == "a\nb"


@pytest.mark.asyncio
async def test_slash_opens_filters_navigates_and_executes_command_popup(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("slash")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert popup.display is True
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/new"

        await pilot.press("down")
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/sessions"

        await pilot.press("escape")
        assert popup.display is False
        assert app.query_one(Composer).text == "/"

        app.query_one(Composer).clear()
        await pilot.press("slash", "h")
        await pilot.pause()
        assert popup.display is True
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/help"

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_approval_shortcut_resumes_the_canonical_run(tmp_path: Path) -> None:
    target = tmp_path / "approved.txt"
    app = OuroTuiApp(
        application=_approval_application(tmp_path, target),
        approval_mode="approval",
    )

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("w", "r", "i", "t", "e", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.pending_interaction is not None
        assert app._projection.pending_interaction.kind == "approval"

        await pilot.press("y")
        await pilot.pause(0.8)

        assert target.read_text(encoding="utf-8") == "written"
        assert app._projection.pending_interaction is None
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_builtin_ask_user_renders_form_and_submits_structured_answer(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await pilot.pause(0.7)

        panel = app.query_one(AskUserPanel)
        assert panel.display is True
        assert panel.has_focus is True
        assert "Which file should OURO use?" in str(panel.render())

        await pilot.press("2")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        assert app._projection.assistant_text == "Answer received."
        assert panel.display is False
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == {"answers": {"file": ["README.md"]}}


@pytest.mark.asyncio
async def test_ask_user_other_accepts_freeform_even_when_answer_starts_with_slash(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path)
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await pilot.pause(0.7)

        await pilot.press("down", "down", "enter")
        await pilot.pause()
        panel = app.query_one(AskUserPanel)
        assert panel.awaiting_freeform is True
        assert app.query_one(Composer).has_focus is True

        await pilot.press("slash", "t", "m", "p", "enter")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == {"answers": {"file": ["/tmp"]}}


@pytest.mark.asyncio
async def test_plugin_question_uses_common_form_and_returns_scalar_answer(
    tmp_path: Path,
) -> None:
    application = _question_application(tmp_path, tool_name="PluginQuestion")
    app = OuroTuiApp(application=application)

    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.pause(0.5)
        await pilot.click("#composer")
        await pilot.press("a", "s", "k", "enter")
        await pilot.pause(0.7)

        panel = app.query_one(AskUserPanel)
        assert panel.display is True
        assert "How should the plugin continue?" in str(panel.render())

        await pilot.press("1")
        await pilot.pause(0.8)

        assert app._projection is not None
        assert app._projection.pending_interaction is None
        events = application.runtime.sessions.event_page(
            app._projection.session_id,
            cursor=0,
            limit=200,
        ).events
        resolved = next(event for event in events if event.event_type == "ask_user_resolved")
        assert resolved.payload["answer"] == "Safely"
