"""TUI application scenarios: preferences."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from rich.console import Console
from textual.widgets import Input, OptionList, Static

import ouro.tui.conversation.history as conversation_history
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord, TextContent
from ouro.tui.app import OuroTuiApp, _session_choices
from ouro.tui.composer.drafts import TuiInputDrafts, TuiInputDraftStore
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.turn import AssistantTurn
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.theme import DEFAULT_TUI_THEME, THEME_SATURATION_LEVELS, TUI_THEMES
from tests.tui.app.support import _application
from tests.tui.helpers import choose_menu, run_ready_app, wait_for_history_render
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_until as _wait_until


def test_session_choices_prefix_forks_with_the_theme_separator(tmp_path: Path) -> None:
    application = _application(tmp_path)
    source = application.runtime.sessions.create(title="Shared title").session
    branch = application.runtime.sessions.branch(source.session_id, None).session

    choices = {
        choice.value: choice
        for choice in _session_choices(application.runtime.sessions.catalog().entries)
    }

    assert choices[source.session_id].label == "Shared title"
    assert choices[branch.session_id].label == (
        f"forked{DEFAULT_TUI_THEME.inline_separator}Shared title"
    )


@pytest.mark.asyncio
async def test_thought_display_command_applies_to_history_and_live_output_and_persists(
    tmp_path: Path,
) -> None:
    message = MessageRecord(
        session_id="ses_thought_display",
        role="assistant",
        content=[TextContent(text="Final answer")],
        reasoning_content="Historical private thought",
    )
    preferences = TuiPreferencesStore(tmp_path)
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages((message,))
        await wait_for_history_render(pilot)
        history = conversation.query_one(ConversationHistory)
        assert "+ Thought" in history.rendered_text
        assert "Historical private thought" not in history.rendered_text
        collapsed_lines = history.rendered_text.splitlines()
        collapsed_thought = next(
            index for index, line in enumerate(collapsed_lines) if line.strip() == "+ Thought"
        )
        collapsed_answer = next(
            index for index, line in enumerate(collapsed_lines) if line.strip() == "Final answer"
        )
        assert collapsed_lines[collapsed_thought + 1] == ""
        assert collapsed_answer == collapsed_thought + 2

        await pilot.press("slash", *"thoughts", "enter")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/thoughts"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "expand"

        await pilot.press("enter")
        await _wait_until(
            lambda: "Historical private thought" in history.rendered_text,
            pilot,
            description="thinking display applied to history",
        )
        assert app.query_one(Composer).text == ""
        assert "- Thought" in history.rendered_text
        assert "Historical private thought" in history.rendered_text
        assert preferences.load().reasoning_expanded is True

        await conversation.append_reasoning("Live private thought")
        live_reasoning = app.query_one(AssistantTurn).reasoning
        assert live_reasoning.text == "Live private thought"
        assert live_reasoning.expanded is True

        await app._dispatch_command("/thoughts collapse")
        await pilot.pause()
        assert "Historical private thought" not in history.rendered_text
        assert live_reasoning.expanded is False
        assert live_reasoning.label.render().plain.startswith("+ Thinking")

        await conversation.append_assistant("Live final answer")
        await pilot.pause()
        live_turn = app.query_one(AssistantTurn)
        assert live_turn.tail.region.y == live_reasoning.label.region.bottom + 1

        app._request_tui_theme("blossom")
        await app.workers.wait_for_complete(
            [worker for worker in app.workers if worker.name == "tui-theme-switch"]
        )
        assert preferences.load() == TuiPreferences(
            theme="blossom",
            reasoning_expanded=False,
            recent_workspaces=(str(tmp_path.resolve()),),
        )

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(restarted, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = restarted.query_one(ConversationView)
        await conversation.load_messages((message,))
        await wait_for_history_render(pilot)
        history = conversation.query_one(ConversationHistory)
        assert "Historical private thought" not in history.rendered_text

        await restarted._dispatch_command("/thoughts expand")
        await pilot.pause()
        assert "Historical private thought" in history.rendered_text
        assert preferences.load().reasoning_expanded is True


@pytest.mark.asyncio
async def test_preference_writes_run_outside_the_event_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)):
        writer_threads: list[int] = []
        save_preferences = app._preferences_store.save

        def record_save(preferences: TuiPreferences) -> None:
            writer_threads.append(threading.get_ident())
            save_preferences(preferences)

        monkeypatch.setattr(app._preferences_store, "save", record_save)
        event_loop_thread = threading.get_ident()
        app._request_tui_theme("blossom")
        saving = next(worker for worker in app.workers if worker.name == "tui-theme-switch")
        await saving.wait()

        assert writer_threads
        assert event_loop_thread not in writer_threads
        assert app._preferences_store.load().theme == "blossom"
        assert app.tui_theme == TUI_THEMES["blossom"]


@pytest.mark.asyncio
async def test_theme_selection_and_saturation_persist_without_committing_preview(tmp_path: Path):
    preferences = TuiPreferencesStore(tmp_path)
    app = OuroTuiApp(application=_application(tmp_path))
    async with run_ready_app(app) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Theme", title="THEME")
        menu = app.screen
        options = menu.query_one(OptionList)
        options.highlighted = options.get_option_index("coral")
        await pilot.pause()
        assert app.tui_theme == TUI_THEMES["original"]
        assert preferences.load().theme == "original"

        await pilot.press("enter")
        await _wait_until(
            lambda: preferences.load().theme == "coral" and not app._theme_switch_active,
            pilot,
            description="saved theme selection",
        )
        assert app.screen is menu
        assert app.tui_theme == TUI_THEMES["coral"]

        await pilot.press("right")
        assert preferences.load().theme_saturation_level == 0
        await pilot.press("enter")
        await _wait_until(
            lambda: preferences.load().theme_saturation_level == 1 and not app._theme_switch_active,
            pilot,
            description="saved saturation selection",
        )
        assert app.tui_theme == TUI_THEMES["coral"].at_saturation_level(1)

    restarted = OuroTuiApp(application=_application(tmp_path))
    assert restarted.tui_theme == TUI_THEMES["coral"].at_saturation_level(1)


@pytest.mark.asyncio
async def test_theme_switch_stays_interactive_coalesces_and_outlives_picker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Theme", title="THEME")

        theme_screen = app.screen
        assert isinstance(theme_screen, ChoiceScreen)
        options = theme_screen.query_one(OptionList)
        options.highlighted = options.get_option_index("original")
        entered = threading.Event()
        release = threading.Event()
        saved_themes: list[str] = []
        save_preferences = app._preferences_store.save
        event_loop_thread = threading.get_ident()

        def blocking_save(preferences: TuiPreferences) -> None:
            # Fail instead of deadlocking if saving regresses to the UI thread.
            assert threading.get_ident() != event_loop_thread
            saved_themes.append(preferences.theme)
            if len(saved_themes) == 1:
                entered.set()
                release.wait()
            save_preferences(preferences)

        monkeypatch.setattr(app._preferences_store, "save", blocking_save)
        # Keep I/O blocked until all pending-state assertions have run, regardless
        # of machine speed; finally also releases the thread after an assertion fails.
        try:
            await pilot.press("down", "enter")
            await _wait_until(entered.is_set, pilot, description="theme save blocked")
            choices = {choice.value: choice for choice in theme_screen._choices}
            assert choices["blossom"].loading is True
            assert choices["coral"].loading is False
            assert app.tui_theme == TUI_THEMES["original"]

            await pilot.press("down", "enter")
            await pilot.pause()
            choices = {choice.value: choice for choice in theme_screen._choices}
            assert choices["blossom"].loading is False
            assert choices["coral"].loading is True
            assert app.tui_theme == TUI_THEMES["original"]

            reasoning_update = asyncio.create_task(app._set_reasoning_expanded("expand"))
            await pilot.pause()
            assert reasoning_update.done() is False

            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
            assert app.screen is theme_screen
            assert theme_screen.page_title == "ACTIONS"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not theme_screen
            assert app._theme_switch_active is True
        finally:
            release.set()

        await _wait_until(
            lambda: not app._theme_switch_active,
            pilot,
            description="coalesced background theme switch",
        )
        assert await reasoning_update is True
        assert saved_themes[0] == "blossom"
        assert saved_themes[-1] == "coral"
        assert app.tui_theme == TUI_THEMES["coral"]
        stored_preferences = TuiPreferencesStore(tmp_path).load()
        assert stored_preferences.theme == "coral"
        assert stored_preferences.reasoning_expanded is True


@pytest.mark.asyncio
async def test_theme_loading_ends_after_history_reaches_the_new_theme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    message = MessageRecord(
        session_id="ses_theme_loading",
        role="assistant",
        content=[TextContent(text="```python\nvalue = 1\n```\n")],
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await app.query_one(ConversationView).load_messages((message,))
        await wait_for_history_render(pilot)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Theme", title="THEME")

        theme_screen = app.screen
        assert isinstance(theme_screen, ChoiceScreen)
        options = theme_screen.query_one(OptionList)
        options.highlighted = options.get_option_index("original")
        entered = threading.Event()
        release = threading.Event()
        render_entries = conversation_history._render_entry_requests
        event_loop_thread = threading.get_ident()

        def blocking_render(*args, **kwargs):
            assert threading.get_ident() != event_loop_thread
            entered.set()
            release.wait()
            return render_entries(*args, **kwargs)

        monkeypatch.setattr(
            conversation_history,
            "_render_entry_requests",
            blocking_render,
        )
        try:
            await pilot.press("down", "enter")
            await _wait_until(entered.is_set, pilot, description="theme history render blocked")
            await pilot.pause()
            assert app.tui_theme == TUI_THEMES["blossom"]
            assert app._theme_switch_active is True
            assert (
                next(
                    choice for choice in theme_screen._choices if choice.value == "blossom"
                ).loading
                is True
            )
            loading_prompt = theme_screen.query_one(OptionList).get_option("blossom").prompt
            loading_line = "".join(
                segment.text for segment in Console(width=60).render(loading_prompt)
            ).splitlines()[0]
            assert loading_line[1:3] == "  "
            assert loading_line[3 : 3 + THEME_SATURATION_LEVELS] == (
                DEFAULT_TUI_THEME.selected_session_node_mark
                + DEFAULT_TUI_THEME.session_node_mark * (THEME_SATURATION_LEVELS - 1)
            )
        finally:
            release.set()

        await _wait_until(
            lambda: not app._theme_switch_active,
            pilot,
            description="theme history render",
        )
        assert not any(choice.loading for choice in theme_screen._choices)


@pytest.mark.asyncio
async def test_theme_persistence_failure_keeps_current_theme_and_clears_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32), notifications=True) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Theme", title="THEME")
        theme_screen = app.screen
        assert isinstance(theme_screen, ChoiceScreen)

        def fail_save(_preferences: TuiPreferences) -> None:
            raise PersistenceError("theme storage unavailable")

        monkeypatch.setattr(app._preferences_store, "save", fail_save)
        options = theme_screen.query_one(OptionList)
        options.highlighted = options.get_option_index("original")
        await pilot.press("down", "enter")
        await _wait_until(
            lambda: not app._theme_switch_active,
            pilot,
            description="failed theme switch",
        )
        await pilot.pause()

        assert app.tui_theme == TUI_THEMES["original"]
        assert TuiPreferencesStore(tmp_path).load().theme == "original"
        assert not any(choice.loading for choice in theme_screen._choices)
        assert list(theme_screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_action_menu_updates_thought_display_without_closing_picker(
    tmp_path: Path,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32), notifications=True) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Thinking display", title="THINKING DISPLAY")

        thought_screen = app.screen
        assert isinstance(thought_screen, ChoiceScreen)
        assert thought_screen.query_one("#choice-title", Static).render().plain == (
            "THINKING DISPLAY"
        )
        choices = {choice.value: choice for choice in thought_screen._choices}
        assert choices["collapse"].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices["expand"].marker == ""

        await choose_menu(pilot, "Expand")
        await _wait_until(
            lambda: any(
                choice.value == "expand"
                and choice.marker == DEFAULT_TUI_THEME.selected_choice_marker
                for choice in thought_screen._choices
            ),
            pilot,
            description="thinking display saved and selected row refreshed",
        )
        assert app.screen is thought_screen
        choices = {choice.value: choice for choice in thought_screen._choices}
        assert choices["expand"].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices["collapse"].marker == ""
        assert preferences.load().reasoning_expanded is True
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))

        await pilot.press("escape")


@pytest.mark.asyncio
async def test_action_menu_toggles_input_draft_memory_and_clears_disabled_state(
    tmp_path: Path,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    drafts = TuiInputDraftStore(tmp_path)
    drafts.save(TuiInputDrafts().remember(None, "remember this draft"))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.text == "remember this draft"

        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Remember drafts", title="REMEMBER DRAFTS")

        memory_screen = app.screen
        assert isinstance(memory_screen, ChoiceScreen)
        assert memory_screen.query_one("#choice-title", Static).render().plain == (
            "REMEMBER DRAFTS"
        )
        choices = {choice.value: choice for choice in memory_screen._choices}
        assert choices["enable"].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices["disable"].marker == ""

        memory_screen.query_one("#choice-filter", Input).value = "Do not save drafts"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_until(
            lambda: drafts.load() == TuiInputDrafts(),
            pilot,
            description="disabled draft memory to clear durable drafts",
        )
        assert app.screen is memory_screen
        assert preferences.load().input_draft_memory_enabled is False
        assert composer.text == "remember this draft"

        memory_screen.query_one("#choice-filter", Input).value = "Remember drafts"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_until(
            lambda: drafts.load().draft_for(None) == "remember this draft",
            pilot,
            description="re-enabled draft memory to remember the visible draft",
        )
        choices = {choice.value: choice for choice in memory_screen._choices}
        assert choices["enable"].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices["disable"].marker == ""
        assert preferences.load().input_draft_memory_enabled is True


@pytest.mark.asyncio
async def test_theme_preview_is_local_and_cleared_when_picker_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferences = TuiPreferencesStore(tmp_path)
    preferences.save(TuiPreferences(theme="sienna"))
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Theme", title="THEME")
        theme_screen = app.screen
        assert isinstance(theme_screen, ChoiceScreen)
        theme_options = theme_screen.query_one(OptionList)
        theme_options.highlighted = theme_options.get_option_index("sienna")

        await pilot.press("down")
        await pilot.pause()

        assert app.tui_theme == TUI_THEMES["sienna"]

        theme_options = theme_screen.query_one(OptionList)
        honey_index = theme_options.highlighted
        enabled_indices = [
            index for index, option in enumerate(theme_options.options) if not option.disabled
        ]
        clear_calls = 0
        update_calls = 0
        clear_options = theme_options.clear_options
        update_choices = theme_screen.update_choices

        def counted_clear_options():
            nonlocal clear_calls
            clear_calls += 1
            return clear_options()

        def counted_update_choices(*args, **kwargs):
            nonlocal update_calls
            update_calls += 1
            return update_choices(*args, **kwargs)

        monkeypatch.setattr(theme_options, "clear_options", counted_clear_options)
        monkeypatch.setattr(theme_screen, "update_choices", counted_update_choices)
        repeated_moves = len(enabled_indices) * 8 + 7
        for _ in range(repeated_moves):
            theme_screen.action_next_choice()
        await pilot.pause()

        expected_index = enabled_indices[
            (enabled_indices.index(honey_index) + repeated_moves) % len(enabled_indices)
        ]
        assert theme_options.highlighted == expected_index
        assert clear_calls == 0
        assert update_calls == 1
        assert app.tui_theme == TUI_THEMES["sienna"]

        for _ in range(repeated_moves):
            theme_screen.action_previous_choice()
        await pilot.pause()

        assert theme_options.highlighted == honey_index
        assert clear_calls == 0
        assert update_calls == 2
        assert app.tui_theme == TUI_THEMES["sienna"]

        await pilot.press("escape")
        await pilot.pause()

        assert app.tui_theme == TUI_THEMES["sienna"]
        next(choice for choice in app.screen._choices if choice.label == "Theme")
        assert preferences.load() == TuiPreferences(
            theme="sienna",
            recent_actions=("__ouro_theme__",),
            recent_workspaces=(str(tmp_path.resolve()),),
        )
