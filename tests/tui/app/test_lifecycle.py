"""TUI application scenarios: lifecycle."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from textual import events
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static, TextArea

import ouro.tui.conversation.markdown_stream as conversation_history_markdown_stream
from ouro.application.configuration.facade import ConfigurationFacade
from ouro.application.contracts.agents import AgentTreeNode
from ouro.core.events import EventRecord
from ouro.local_application import LocalApplication
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.turn import AssistantTurn
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.state import TuiSessionProjection
from ouro.tui.widgets.activity import ActivityIndicator
from tests.tui.app.support import _application, _persist_direct_child_session, _thinking_application
from tests.tui.helpers import resize_terminal, run_ready_app
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_long_live_markdown_closes_without_blocking_the_ui_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.append_assistant("```python\n")
        await conversation.append_assistant("x" * 4_096)
        entered = threading.Event()
        release = threading.Event()
        parser_threads: list[int] = []
        block_starts = conversation_history_markdown_stream._block_starts

        def blocking_block_starts(source: str) -> list[int]:
            parser_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return block_starts(source)

        monkeypatch.setattr(
            conversation_history_markdown_stream, "_block_starts", blocking_block_starts
        )
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        event_loop_thread = threading.get_ident()
        finishing = asyncio.create_task(conversation.finish_assistant())

        try:
            parser_entered = await asyncio.to_thread(entered.wait, 2)
            event_loop_remained_responsive = not finishing.done()
        finally:
            release.set()
            watchdog.cancel()
        await finishing

        assert parser_entered is True
        assert event_loop_remained_responsive is True
        assert parser_threads != [event_loop_thread]
        assert not conversation.query_one(AssistantTurn).display


@pytest.mark.asyncio
async def test_visible_subscription_owns_events_without_global_sink_duplicates(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Observed once").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app._visible_session_id() == session.session_id,
            pilot,
            description="visible subscribed Session",
        )
        await _wait_until(
            lambda: not app._pending_runtime_events,
            pilot,
            description="settled event buffer",
        )
        assert app._observation_subscription is not None
        event = EventRecord(
            session_id=session.session_id,
            run_id="run_duplicate",
            event_type="model_text_delta",
            sequence=1,
            payload={"text": "duplicate sink delivery"},
        )

        await app._controller._receive_event(event)

        assert app._pending_runtime_events == []


@pytest.mark.asyncio
async def test_tui_runtime_event_staging_applies_one_bounded_prefix_per_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ouro.tui.app._RUNTIME_EVENT_APPLY_MAX", 2)
    app = OuroTuiApp(application=_application(tmp_path))
    applied: list[tuple[EventRecord, ...]] = []

    async def apply(events):
        applied.append(tuple(events))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        events = tuple(
            EventRecord(
                session_id="ses_prefix",
                run_id="run_prefix",
                event_type="model_text_delta",
                sequence=sequence,
                payload={"text": str(sequence)},
            )
            for sequence in range(1, 4)
        )
        monkeypatch.setattr(app, "_apply_runtime_events", apply)
        app._pending_runtime_events.extend(events)

        await app._flush_pending_runtime_events()

        assert applied == [events[:2]]
        assert app._pending_runtime_events == [events[2]]
        assert app._runtime_events_frame_scheduled is not None
        app._pending_runtime_events.clear()


@pytest.mark.asyncio
async def test_tui_runtime_event_staging_preserves_a_streamed_tool_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    applied: list[tuple[EventRecord, ...]] = []

    async def apply(events):
        applied.append(tuple(events))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        events = (
            EventRecord(
                session_id="ses_tool_frame",
                run_id="run_tool_frame",
                event_type="model_tool_call_delta",
                sequence=1,
                payload={"turn_id": "turn_1", "index": 0, "name_delta": "Write"},
            ),
            EventRecord(
                session_id="ses_tool_frame",
                run_id="run_tool_frame",
                event_type="model_tool_call_delta",
                sequence=2,
                payload={"turn_id": "turn_1", "index": 0, "arguments_delta": "{}"},
            ),
            EventRecord(
                session_id="ses_tool_frame",
                run_id="run_tool_frame",
                event_type="model_message_completed",
                sequence=3,
                payload={"tool_call_count": 1},
            ),
        )
        monkeypatch.setattr(app, "_apply_runtime_events", apply)
        app._pending_runtime_events.extend(events)

        await app._flush_pending_runtime_events()

        assert applied == [events[:2]]
        assert app._pending_runtime_events == [events[2]]
        assert app._runtime_events_frame_scheduled is not None
        app._pending_runtime_events.clear()


@pytest.mark.asyncio
async def test_runtime_event_prefix_mounts_one_complete_tool_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    session_id = "ses_tool_batch_prefix"
    run_id = "run_tool_batch_prefix"
    app._projection = TuiSessionProjection(session_id)
    events = (
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="run_started",
            sequence=1,
            payload={"trigger": "user"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="model_request_started",
            sequence=2,
            payload={"turn_id": "turn_tool_batch_prefix"},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="model_text_delta",
            sequence=3,
            payload={"text": "Inspecting the batch."},
        ),
        EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="tool_batch_planned",
            sequence=4,
            payload={"batch_id": "batch_prefix"},
        ),
        *(
            EventRecord(
                session_id=session_id,
                run_id=run_id,
                event_type="tool_call_requested",
                sequence=5 + index,
                payload={
                    "tool_call_id": f"call_prefix_{index}",
                    "tool_name": f"Tool{index}",
                    "source_index": index,
                    "arguments": {"index": index},
                },
            )
            for index in range(4)
        ),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        mounted_batches: list[tuple[str, ...]] = []
        mount_all = conversation._transcript.mount_all

        async def record_mount_all(widgets, **kwargs):
            rows = tuple(widgets)
            mounted_batches.append(tuple(row.tool_call_id for row in rows))
            await mount_all(rows, **kwargs)

        monkeypatch.setattr(conversation._transcript, "mount_all", record_mount_all)
        await app._apply_runtime_events(events)

        assert mounted_batches == [tuple(f"call_prefix_{index}" for index in range(4))]
        assert len(conversation.query(".tool-row")) == 4


@pytest.mark.asyncio
async def test_agent_node_navigation_finishes_current_load_before_latest_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    root_session_id, child_session_id = _persist_direct_child_session(application)
    app = OuroTuiApp(
        application=application,
        resume_session_id=root_session_id,
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await _wait_until(
            lambda: app._visible_session_id() == root_session_id,
            pilot,
            description="root surface before coalesced agent navigation",
        )
        original_open_child = app._open_child_session
        original_show_main = app._show_main_session
        release_child = asyncio.Event()
        child_started = asyncio.Event()
        child_finished = asyncio.Event()
        main_started = asyncio.Event()
        order: list[str] = []
        active_loads = 0
        peak_active_loads = 0

        async def delayed_open_child(agent: AgentTreeNode) -> None:
            nonlocal active_loads, peak_active_loads
            active_loads += 1
            peak_active_loads = max(peak_active_loads, active_loads)
            order.append(agent.session_id)
            child_started.set()
            try:
                await release_child.wait()
                await original_open_child(agent)
                child_finished.set()
            finally:
                active_loads -= 1

        async def observed_show_main() -> None:
            nonlocal active_loads, peak_active_loads
            active_loads += 1
            peak_active_loads = max(peak_active_loads, active_loads)
            order.append(root_session_id)
            main_started.set()
            try:
                await original_show_main()
            finally:
                active_loads -= 1

        monkeypatch.setattr(app, "_open_child_session", delayed_open_child)
        monkeypatch.setattr(app, "_show_main_session", observed_show_main)

        app.post_message(events.Key("alt+right", None))
        await asyncio.wait_for(child_started.wait(), timeout=2)
        app.post_message(events.Key("alt+up", None))
        await pilot.pause()

        assert main_started.is_set() is False
        release_child.set()
        await _wait_for_worker_group(app, pilot, group="navigation")

        assert child_finished.is_set() is True
        assert order == [child_session_id, root_session_id]
        assert peak_active_loads == 1
        assert app._visible_session_id() == root_session_id


@pytest.mark.asyncio
async def test_enabled_without_effort_shows_the_resolved_default_level(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)
        # The endpoint default resolves to enabled + low, shown as "low".
        assert indicator._reasoning == "low"

        # Enabled is canonicalized to the endpoint's configured default effort.
        await pilot.press("slash", *"thinking", "enter", *"enabled", "enter")
        await pilot.pause()
        assert indicator._reasoning == "low"
        assert indicator._reasoning_peak is False
        assert "low" in indicator.render().plain


@pytest.mark.asyncio
async def test_shared_menu_height_tracks_terminal_with_bounded_layout(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 60)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        panel = app.screen.query_one("#choice-panel", Vertical)
        choices = app.screen.query_one("#choice-list", OptionList)
        hint = app.screen.query_one("#choice-hint", Static)
        assert app.screen.styles.overflow_y == "hidden"
        assert panel.region.height == 27
        assert panel.region.x == app.screen.region.width - panel.region.right
        assert panel.region.bottom <= app.screen.region.height
        assert choices.region.bottom <= panel.region.bottom
        assert hint.region.bottom <= panel.region.bottom

        app.screen.query_one("#choice-filter", Input).value = "Keyboard help"
        await pilot.pause()
        assert panel.region.height == 12
        app.screen.query_one("#choice-filter", Input).value = ""
        await pilot.pause()

        await resize_terminal(pilot, 48, 18)
        await pilot.pause()
        assert 12 <= panel.region.height < 27
        assert panel.region.x == app.screen.region.width - panel.region.right
        assert choices.region.height >= 5
        assert panel.region.bottom <= app.screen.region.height
        assert hint.region.bottom <= panel.region.bottom

        await resize_terminal(pilot, 48, 12)
        await pilot.pause()
        assert panel.region.height == 12
        assert panel.region.x == app.screen.region.width - panel.region.right
        assert choices.region.height == 5
        assert panel.region.bottom <= app.screen.region.height
        assert hint.region.bottom <= panel.region.bottom


@pytest.mark.asyncio
async def test_unselected_real_model_keeps_draft_instead_of_running_faux(tmp_path: Path) -> None:
    ConfigurationFacade(tmp_path).add_provider(display_name="Primary", provider_type="openai")
    app = OuroTuiApp(application=LocalApplication.build(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Choose a real model first")
        await pilot.press("enter")
        await pilot.pause()

        assert app._controller.session_id is None
        assert composer.text == "Choose a real model first"
        assert "faux" not in app.query_one(ActivityIndicator).render().plain.casefold()


@pytest.mark.asyncio
async def test_alt_navigation_shortcuts_preempt_all_focused_text_inputs(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", TextArea)
        composer.text = "draft"
        composer.focus()
        app.post_message(events.Key("alt+x", "x"))
        await pilot.pause()
        assert composer.text == "draft"

        app.post_message(events.Key("alt+p", "p"))
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")

        assert {choice.value for choice in app.screen._choices} >= {
            "dev.ouro.agent_context",
            "dev.ouro.plan",
        }
        assert composer.text == "draft"

        plugin_screen = app.screen
        plugin_filter = plugin_screen.query_one("#choice-filter", Input)
        plugin_filter.value = "tool"
        plugin_filter.focus()
        app.post_message(events.Key("alt+x", "x"))
        await pilot.pause()
        assert app.screen is plugin_screen
        assert plugin_filter.value == "tool"

        app.post_message(events.Key("alt+p", "p"))
        await pilot.pause()

        assert app.screen is plugin_screen
        assert plugin_filter.value == "tool"
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1


@pytest.mark.asyncio
async def test_alt_menu_shortcuts_are_idempotent_and_do_not_stack_screens(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("alt+p")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")
        menu = app.screen

        for _ in range(3):
            await pilot.press("alt+p")
            await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")

        assert app.screen is menu
        assert len(menu._page_stack) == 0
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1


@pytest.mark.asyncio
async def test_navigation_menu_routes_ctrl_c_to_abort_without_closing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    aborted = False

    def abort() -> None:
        nonlocal aborted
        aborted = True

    monkeypatch.setattr(app, "_has_active_run", lambda: True)
    monkeypatch.setattr(app, "_abort_run", abort)

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        copied: list[str] = []
        monkeypatch.setattr(app.screen_stack[0], "_clipboard_writer", copied.append)
        filter_input = menu.query_one(Input)
        filter_input.value = "Theme"
        await pilot.press("ctrl+a", "ctrl+c")
        assert copied == ["Theme"]
        assert app.clipboard == "Theme"
        assert aborted is False
        await pilot.press("end")

        await pilot.press("ctrl+c")
        await pilot.pause()

        assert aborted is True
        assert app.screen is menu


@pytest.mark.parametrize("submit", (False, True))
async def test_model_selection_wait_does_not_block_new_input(tmp_path, monkeypatch, submit):
    app = OuroTuiApp(application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    submitted = asyncio.Event()
    async with run_ready_app(app) as pilot:
        select_model = app._controller.select_model
        submit_prompt = app._controller.submit

        async def record_submit(*args, **kwargs):
            submitted.set()
            return await submit_prompt(*args, **kwargs)

        async def slow_select(value):
            entered.set()
            await release.wait()
            return await select_model(value)

        monkeypatch.setattr(app._controller, "select_model", slow_select)
        monkeypatch.setattr(app._controller, "submit", record_submit)
        await pilot.press("slash", *"model", "enter")
        pending = asyncio.create_task(pilot.press("enter"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await asyncio.wait_for(pending, timeout=2)
            await pilot.press(*"Next prompt")
            assert app.query_one(Composer).text == "Next prompt"
            assert "Working" in app.query_one(ActivityIndicator).render().plain
            if submit:
                await asyncio.wait_for(pilot.press("enter", *"Follow-up"), timeout=2)
                assert not submitted.is_set()
                assert app.query_one(Composer).text == "Follow-up"
        finally:
            release.set()
        await app.workers.wait_for_complete(
            [
                worker
                for worker in app.workers
                if worker.group in {"composer-command", "composer-input"}
            ]
        )
        if submit:
            await asyncio.wait_for(submitted.wait(), timeout=2)
        await pilot.pause()
        assert app.query_one(Composer).text == ("Follow-up" if submit else "Next prompt")
        assert "Working" not in app.query_one(ActivityIndicator).render().plain


@pytest.mark.asyncio
async def test_plan_status_keeps_its_conversation_notice(tmp_path: Path) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    assert (await application.plugins.enable("dev.ouro.plan")).status == "active"
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"plan status", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")
        await app.query_one(ConversationHistory).wait_until_rendered()

        notices = tuple(
            entry.text
            for entry in app.query_one(ConversationHistory).entries
            if entry.kind == "notice"
        )
        assert notices == ("Plan mode phase: inactive.",)
        assert "▣ Plan mode phase: inactive." in "\n".join(
            line.text for line in app.query_one(ConversationHistory).lines
        )
        assert app.query_one(ConversationView).showing_welcome is False


@pytest.mark.asyncio
async def test_noop_abort_does_not_notify_when_ready_status_is_visible(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert "Ready" in app.query_one(ActivityIndicator).render().plain
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))
