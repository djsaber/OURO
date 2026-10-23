"""TUI application scenarios: composer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual import events
from textual.content import Content
from textual.widgets import TextArea

import ouro.tui.app as tui_app_module
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.drafts import TuiInputDrafts, TuiInputDraftStore
from ouro.tui.composer.input import (
    COMPOSER_FRAME_HEIGHT,
    COMPOSER_MAX_HEIGHT,
    COMPOSER_MIN_HEIGHT,
    COMPOSER_TEXT_MAX_HEIGHT,
    Composer,
)
from ouro.tui.composer.popup import COMMAND_POPUP_MAX_HEIGHT, CommandPopup
from ouro.tui.conversation.view import ConversationView
from ouro.tui.interactions.approval import INTERACTION_PANEL_MAX_HEIGHT
from ouro.tui.interactions.bar import InteractionBar
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.widgets.status import StatusLine
from tests.tui.app.providers import BlockingStreamProvider
from tests.tui.app.support import (
    _application,
    _blocking_application,
    _wait_for_active_session,
    _wait_for_welcome_render,
)
from tests.tui.app.test_navigation import _scrollable_messages
from tests.tui.helpers import resize_terminal, run_ready_app, wait_for_history_render
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_session_input_surface_reserves_one_row_above_connected_menus(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        panel = app.query_one(SubagentNavigationPanel)
        assert panel.display is False

        composer.load_text("start session")
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="run")

        assert panel.display is True
        assert panel.region.height == 1
        assert panel.render().plain.strip() == ""
        assert panel.region.bottom == composer.region.y
        assert panel.content_region.x == composer.region.x
        assert panel.content_region.right == composer.region.right
        composer_y = composer.region.y

        await pilot.press("slash")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert panel.region.bottom == popup.region.y
        assert popup.region.bottom == composer.region.y
        assert composer.region.y == composer_y

        await pilot.press("escape")
        await pilot.pause()
        assert panel.region.bottom == composer.region.y
        assert composer.region.y == composer_y


@pytest.mark.asyncio
async def test_composer_placeholder_tracks_active_run(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.placeholder == "Ask anything..."

        await pilot.click("#composer")
        try:
            await pilot.press(*"stream", "enter")
            await _wait_until(
                lambda: (
                    provider.first_chunk_sent.is_set()
                    and isinstance(composer.placeholder, Content)
                    and composer.placeholder.plain == Composer.ACTIVE_RUN_PLACEHOLDER
                ),
                pilot,
                description="active-run composer placeholder",
            )
            active_placeholder = composer.placeholder
            assert isinstance(active_placeholder, Content)
            assert active_placeholder.plain == Composer.ACTIVE_RUN_PLACEHOLDER
            for shortcut in ("Enter", "Ctrl+G"):
                style = active_placeholder.get_style_at_offset(
                    active_placeholder.plain.index(shortcut)
                )
                assert style.bold is False

            composer.focus()
            composer.cursor_blink = True
            composer._cursor_visible = False
            cursor_off = composer.render_line(0)
            composer._cursor_visible = True
            cursor_on = composer.render_line(0)
            cursor_off_style = next(iter(cursor_off.crop(0, 1))).style
            cursor_on_style = next(iter(cursor_on.crop(0, 1))).style
            assert cursor_on_style != cursor_off_style
            assert next(iter(cursor_on.crop(1, 2))).style == next(iter(cursor_off.crop(1, 2))).style
        finally:
            provider.release.set()
        await _wait_for_worker_group(app, pilot, group="run")
        await _wait_until(
            lambda: (
                app._projection is not None
                and app._projection.activity.status == "completed"
                and composer.placeholder == Composer.DEFAULT_PLACEHOLDER
                and not app._pending_runtime_events
                and not app._runtime_events_ready_posted
                and not app._runtime_events_frame_scheduled
            ),
            pilot,
            description="completed streamed run with settled TUI events",
        )

        assert app._projection is not None
        assert app._projection.activity.status == "completed"
        assert composer.placeholder == "Ask anything..."


@pytest.mark.asyncio
async def test_conversation_shortcuts_navigate_while_composer_is_focused(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await wait_for_history_render(pilot)
        composer = app.query_one(Composer)
        composer.load_text("draft")
        composer.focus()
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y

        await pilot.press("alt+home")
        await pilot.pause()
        assert conversation.scroll_y == 0

        await pilot.press("alt+end")
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y
        assert composer.text == "draft"
        composer.move_cursor(composer.document.end)
        await pilot.press("ctrl+left")
        assert composer.cursor_location == (0, 0)
        await pilot.press("ctrl+right")
        assert composer.cursor_location == (0, len("draft"))
        assert conversation.scroll_y == conversation.max_scroll_y


@pytest.mark.asyncio
async def test_composer_uses_ctrl_enter_for_newline(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("a", "ctrl+enter", "b", "ctrl+j", "c")

        composer = app.query_one(Composer)
        assert composer.text == "a\nb\nc"
        await pilot.press("ctrl+a", "ctrl+enter")
        assert composer.text == "\n"


@pytest.mark.asyncio
async def test_composer_grows_with_its_document_between_one_and_three_text_rows(
    tmp_path: Path,
) -> None:
    """The composer is a textarea bounded to three text rows, then scrolls."""

    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        composer = app.query_one(Composer)
        status = app.query_one(StatusLine)

        # Empty and single-line documents both keep the minimum single text row.
        assert composer.outer_size.height == COMPOSER_MIN_HEIGHT
        assert composer.size.height == 1
        await pilot.press("a")
        await pilot.pause()
        assert composer.outer_size.height == COMPOSER_MIN_HEIGHT

        for text_rows in (2, 3):
            await pilot.press("ctrl+enter", "a")
            await pilot.pause()
            assert composer.size.height == text_rows
            assert composer.outer_size.height == text_rows + COMPOSER_FRAME_HEIGHT
            # The blank frame row above and below the text survives every step.
            assert composer.content_region.y - composer.region.y == 1
            assert composer.region.bottom - composer.content_region.bottom == 1
            # The status line stays docked directly beneath the grown composer.
            assert status.region.y == composer.region.bottom

        # Past the cap the document scrolls inside a stable widget height.
        await pilot.press("ctrl+enter", "a")
        await pilot.pause()
        assert composer.text.count("\n") == 3
        assert composer.size.height == COMPOSER_TEXT_MAX_HEIGHT
        assert composer.outer_size.height == COMPOSER_MAX_HEIGHT
        assert composer.virtual_size.height == 4
        assert composer.cursor_screen_offset.y == composer.content_region.bottom - 1

        # Removing rows releases the height again, back to the single-row floor.
        composer.load_text("")
        await pilot.pause()
        assert composer.outer_size.height == COMPOSER_MIN_HEIGHT
        assert status.region.y == composer.region.bottom


@pytest.mark.asyncio
async def test_composer_growth_is_driven_by_soft_wrapped_width(tmp_path: Path) -> None:
    """Wrapping is the library's measurement, so narrowing alone grows the box."""

    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("w " * 30)
        await pilot.pause()
        assert composer.outer_size.height == COMPOSER_MIN_HEIGHT

        await resize_terminal(pilot, 30, 32)
        await pilot.pause()
        assert composer.size.height > 1
        assert composer.outer_size.height == composer.size.height + COMPOSER_FRAME_HEIGHT
        assert composer.outer_size.height <= COMPOSER_MAX_HEIGHT
        assert app.query_one(StatusLine).region.y == composer.region.bottom


@pytest.mark.asyncio
async def test_growing_composer_rebounds_adjacent_panels_and_conversation(
    tmp_path: Path,
) -> None:
    """Sibling chrome keeps its bounds relative to the composer's live height."""

    app = OuroTuiApp(application=_application(tmp_path))

    # A short viewport keeps both panels bound by the available height rather
    # than by their own caps, so the composer's growth is the only variable.
    async with run_ready_app(app, size=(100, 15)) as pilot:
        await pilot.pause()
        # Leave the centered welcome page for the regular conversation surface.
        app._set_welcome_mode(False)
        await pilot.pause()
        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        interaction_bar = app.query_one(InteractionBar)
        conversation = app.query_one(ConversationView)

        def bounds() -> tuple[int, int, int]:
            return (
                popup.styles.max_height.cells,
                interaction_bar.styles.max_height.cells,
                conversation.region.height,
            )

        popup_before, interaction_before, conversation_before = bounds()
        assert popup_before < COMMAND_POPUP_MAX_HEIGHT
        assert interaction_before < INTERACTION_PANEL_MAX_HEIGHT

        composer.load_text("a\nb\nc")
        await pilot.pause()
        assert composer.outer_size.height == COMPOSER_MAX_HEIGHT

        popup_after, interaction_after, conversation_after = bounds()
        grown = COMPOSER_MAX_HEIGHT - COMPOSER_MIN_HEIGHT
        assert popup_after == popup_before - grown
        assert interaction_after == interaction_before - grown
        # The conversation absorbs the growth, so the surface never overflows.
        assert conversation_after == conversation_before - grown
        # The fixed navigation row still separates conversation from composer.
        navigation = app.query_one(SubagentNavigationPanel)
        assert navigation.region.y == conversation.region.bottom
        assert composer.region.y == navigation.region.bottom

        composer.load_text("")
        await pilot.pause()
        assert bounds() == (popup_before, interaction_before, conversation_before)


@pytest.mark.asyncio
async def test_welcome_mode_keeps_growing_composer_centered(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 40)) as pilot:
        welcome = await _wait_for_welcome_render(app, pilot, contains="Commands")
        composer = app.query_one(Composer)

        for text in ("", "a\nb", "a\nb\nc"):
            composer.load_text(text)
            await pilot.pause()

            assert composer.region.y == welcome.page._top.region.bottom
            assert abs(welcome.page._top.region.height - welcome.page._bottom.region.height) <= 1
            assert app.query_one(StatusLine).region.y == composer.region.bottom
            assert welcome.page._system.region.bottom <= welcome.page._bottom.region.bottom


@pytest.mark.asyncio
async def test_input_drafts_restore_per_surface_across_tui_restarts(tmp_path: Path) -> None:
    application = _application(tmp_path)
    first = application.runtime.sessions.create(session_id="ses_first_draft", title="First").session
    second = application.runtime.sessions.create(
        session_id="ses_second_draft",
        title="Second",
    ).session
    for session, text in ((first, "First message"), (second, "Second message")):
        application.runtime.store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                role="user",
                text=text,
                sequence=1,
            )
        )

    draft_store = TuiInputDraftStore(tmp_path)
    seeded = (
        TuiInputDrafts()
        .remember(None, "welcome seed")
        .remember(first.session_id, "first seed")
        .remember(second.session_id, "second seed")
    )
    draft_store.save(seeded)

    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        assert composer.text == "welcome seed"

        composer.load_text("welcome edited")
        await _wait_until(
            lambda: draft_store.load().draft_for(None) == "welcome edited",
            pilot,
            description="persisted welcome draft edit",
        )

        await app._open_session(first.session_id)
        await pilot.pause()
        assert composer.text == "first seed"
        composer.load_text("first edited")
        await _wait_until(
            lambda: draft_store.load().draft_for(first.session_id) == "first edited",
            pilot,
            description="persisted first Session draft edit",
        )

        await app._open_session(second.session_id)
        await pilot.pause()
        assert composer.text == "second seed"
        composer.load_text("second edited")
        await _wait_until(
            lambda: draft_store.load().draft_for(second.session_id) == "second edited",
            pilot,
            description="persisted second Session draft edit",
        )

        await app._open_session(first.session_id)
        await pilot.pause()
        assert composer.text == "first edited"
        await app._new_session()
        await pilot.pause()
        assert composer.text == "welcome edited"

    restarted = OuroTuiApp(application=_application(tmp_path))
    async with run_ready_app(restarted, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = restarted.query_one(Composer)
        assert composer.text == "welcome edited"

        await restarted._open_session(first.session_id)
        await pilot.pause()
        assert composer.text == "first edited"
        composer.clear()
        await _wait_until(
            lambda: draft_store.load().draft_for(first.session_id) == "",
            pilot,
            description="removed first Session draft after editing it empty",
        )

        await restarted._open_session(second.session_id)
        await pilot.pause()
        assert composer.text == "second edited"
        await pilot.press("enter")
        await _wait_for_worker_group(restarted, pilot, group="run")
        await _wait_until(
            lambda: draft_store.load().draft_for(second.session_id) == "",
            pilot,
            description="removed submitted Session draft",
        )

    resumed = OuroTuiApp(
        application=_application(tmp_path),
        resume_session_id=first.session_id,
    )
    async with run_ready_app(resumed, size=(100, 32)) as pilot:
        await _wait_for_active_session(resumed, pilot, first.session_id)
        assert resumed.query_one(Composer).text == ""
        await resumed._new_session()
        await pilot.pause()
        assert resumed.query_one(Composer).text == "welcome edited"


@pytest.mark.asyncio
async def test_input_draft_persistence_debounces_to_the_latest_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tui_app_module,
        "_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS",
        0.1,
        raising=False,
    )
    app = OuroTuiApp(application=_application(tmp_path))
    saved: list[TuiInputDrafts] = []

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        if app._input_draft_save_task is not None:
            await app._input_draft_save_task
        monkeypatch.setattr(app._input_draft_store, "save", saved.append)

        app._remember_input_draft(None, "a")
        await asyncio.sleep(0.04)
        assert saved == []

        app._remember_input_draft(None, "ab")
        await asyncio.sleep(0.04)
        app._remember_input_draft(None, "abc")
        assert app._input_draft_save_task is not None
        await app._input_draft_save_task

        assert len(saved) == 1
        assert saved[0].draft_for(None) == "abc"


@pytest.mark.asyncio
async def test_tui_close_waits_for_pending_input_draft_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_app_module, "_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS", 0.05)
    app = OuroTuiApp(application=_application(tmp_path))
    saved: list[TuiInputDrafts] = []

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        if app._input_draft_save_task is not None:
            await app._input_draft_save_task
        monkeypatch.setattr(app._input_draft_store, "save", saved.append)
        app._remember_input_draft(None, "saved while closing")

    assert len(saved) == 1
    assert saved[0].draft_for(None) == "saved while closing"


@pytest.mark.asyncio
async def test_input_draft_save_failure_allows_a_later_revision_to_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_app_module, "_INPUT_DRAFT_SAVE_DEBOUNCE_SECONDS", 0.01)
    app = OuroTuiApp(application=_application(tmp_path))
    save_attempts = 0
    saved: list[TuiInputDrafts] = []

    def save(drafts: TuiInputDrafts) -> None:
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise PersistenceError("draft storage unavailable")
        saved.append(drafts)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        if app._input_draft_save_task is not None:
            await app._input_draft_save_task
        monkeypatch.setattr(app._input_draft_store, "save", save)

        app._remember_input_draft(None, "first revision")
        await _wait_until(
            lambda: app._input_draft_save_task is None,
            pilot,
            description="failed draft save task to settle",
        )
        assert app._input_draft_error_notified is True

        app._remember_input_draft(None, "retry revision")
        await _wait_until(
            lambda: bool(saved),
            pilot,
            description="later draft revision to retry persistence",
        )

    assert save_attempts == 2
    assert saved[0].draft_for(None) == "retry revision"


@pytest.mark.asyncio
async def test_question_mark_remains_composer_input_after_help_shortcut_migration(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        composer = app.query_one("#composer", TextArea)
        composer.focus()
        app.post_message(events.Key("question_mark", "?"))
        await pilot.pause()

        assert composer.text == "?"
        assert not isinstance(app.screen, ChoiceScreen)


@pytest.mark.parametrize("close", (False, True))
@pytest.mark.parametrize("new_session", (False, True))
async def test_model_catalog_wait_keeps_composer_responsive(
    tmp_path, monkeypatch, close, new_session
):
    app = OuroTuiApp(application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app) as pilot:
        models = app._models

        async def slow_models():
            entered.set()
            await release.wait()
            return models

        monkeypatch.setattr(app._controller, "models", slow_models)
        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        await pilot.press("slash", *"model")
        composer.load_command("/model ", draft="")
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            if new_session:
                await app._new_session()
                composer.load_command("/model ", draft="")
            await asyncio.wait_for(pilot.press(*"faux"), timeout=2)
            assert composer.text == "/model faux"
            assert popup._loading
            await pilot.press("down", "enter")
            assert composer.text == "/model faux"
            if close:
                await asyncio.wait_for(pilot.press("escape"), timeout=2)
                assert not popup.display
        finally:
            release.set()
        await app.workers.wait_for_complete(
            [worker for worker in app.workers if worker.group == "model-menu"]
        )
        await pilot.pause()
        assert not popup._loading and popup._spinner_timer is None
        if close:
            assert not popup.display
        else:
            assert composer.text == "/model faux"
            assert popup.argument_command.name == "/model"
            assert popup.selected_argument.value == "faux/faux"
