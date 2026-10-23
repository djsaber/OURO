"""TUI application scenarios: sessions."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static

from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.reasoning import ReasoningRequest
from ouro.core.state import SessionRecord
from ouro.tui.app import OuroTuiApp, _session_choices
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.multiple import MultiChoiceScreen
from ouro.tui.state import TuiSessionProjection
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.view_models import TuiSessionControlStatus
from ouro.tui.widgets.activity import ActivityIndicator
from ouro.tui.widgets.status import StatusLine
from tests.tui.app.providers import BlockingStreamProvider
from tests.tui.app.support import (
    _application,
    _blocking_application,
    _thinking_application,
    _wait_for_active_session,
)
from tests.tui.helpers import choose_menu, run_ready_app
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


def test_session_choices_group_by_local_last_activity_date(tmp_path: Path) -> None:
    application = _application(tmp_path)
    application.runtime.sessions.create(session_id="ses_template", title="Template")
    template = application.runtime.sessions.catalog().entries[0]
    today = date(2026, 8, 9)
    local_noon = datetime(2026, 8, 9, 12, tzinfo=UTC)
    ages = (0, 1, 2, 6, 7, 29, 30, 45)
    sessions = tuple(
        template.model_copy(
            update={
                "session_id": f"ses_{age}",
                "title": f"Session {age}",
                "last_activity_at": local_noon - timedelta(days=age),
                "archived": age in {2, 30},
            }
        )
        for age in ages
    )

    choices = _session_choices(sessions, today=today)

    assert [choice.group for choice in choices] == [
        "Today",
        "Yesterday",
        "Previous 7 days",
        "Previous 7 days",
        "Previous 30 days",
        "Previous 30 days",
        "July 2026",
        "June 2026",
    ]
    assert [choice.value for choice in choices[-2:]] == ["ses_30", "ses_45"]


def test_session_choices_mark_only_the_selected_session(tmp_path: Path) -> None:
    application = _application(tmp_path)
    first = application.runtime.sessions.create(session_id="ses_first", title="First").session
    second = application.runtime.sessions.create(session_id="ses_second", title="Second").session
    sessions = application.runtime.sessions.catalog().entries

    choices = {
        choice.value: choice
        for choice in _session_choices(
            sessions,
            selected_session_id=second.session_id,
        )
    }

    assert choices[first.session_id].marker == ""
    assert choices[second.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker


def test_session_choices_keep_catalog_errors_visible_and_ordered(tmp_path: Path) -> None:
    application = _application(tmp_path)
    template = application.runtime.sessions.create(title="Template").session
    entry = application.runtime.sessions.catalog().entries[0]
    sessions = (
        entry.model_copy(update={"session_id": template.session_id, "health": "ok"}),
        entry.model_copy(update={"session_id": "ses_warning", "health": "recoverable"}),
        entry.model_copy(update={"session_id": "ses_failure", "health": "corrupt"}),
        entry.model_copy(
            update={
                "session_id": "ses_index_error",
                "session_kind": None,
                "health": None,
                "catalog_error": "session_kind is missing",
            }
        ),
    )
    projected = _session_choices(sessions)
    choices = {choice.value: choice for choice in projected}
    assert choices["ses_index_error"].trailing == "index error"
    assert choices["ses_index_error"].group == "Today"
    assert projected[-1].value == "ses_index_error"


@pytest.mark.asyncio
async def test_tui_changes_model_and_approval_while_the_session_is_running(tmp_path: Path) -> None:
    provider = BlockingStreamProvider()
    application = _blocking_application(tmp_path, provider)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        try:
            await pilot.press(*"stream", "enter")
            await _wait_until(
                lambda: (
                    provider.first_chunk_sent.is_set()
                    and app._projection is not None
                    and app._projection.activity.status == "running"
                ),
                pilot,
                description="active streamed run",
            )
            await app._select_model("blocking/blocking")
            session_id = app._controller.session_id
            assert session_id is not None
            persisted = application.runtime.store.load_session_record(session_id)
            assert persisted.current_model == "blocking/blocking"

            app.action_approval_settings()
            await _wait_for_screen(app, ChoiceScreen, title="TOOL APPROVAL")
            await pilot.press("escape")

            await app._set_approval_mode("auto")
            persisted = application.runtime.store.load_session_record(session_id)
            assert persisted.approval_mode == "auto"
            assert "auto" in app.query_one(StatusLine).query_one(ActivityIndicator).render().plain
        finally:
            provider.release.set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="completed streamed run",
        )


@pytest.mark.asyncio
async def test_runtime_event_handler_schedules_session_control_refresh_without_awaiting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session_id = application.runtime.sessions.create(title="Control refresh").session.session_id
    app = OuroTuiApp(application=application)
    app._projection = TuiSessionProjection(session_id)
    app._controller._session_id = session_id
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_statuses() -> tuple[TuiSessionControlStatus, ...]:
        entered.set()
        await release.wait()
        return (TuiSessionControlStatus(controller_id="control", label="Control"),)

    monkeypatch.setattr(app._controller, "session_control_statuses", delayed_statuses)
    event = EventRecord(
        session_id=session_id,
        run_id="run_control_refresh",
        event_type="tool_call_completed",
        sequence=1,
        payload={"tool_call_id": "call_control_refresh"},
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        applying = asyncio.create_task(app._apply_runtime_events((event,)))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            # Subscription initialization can await its replay read, while the
            # deliberately blocked status refresh must remain independent.
            await asyncio.wait_for(applying, timeout=1)
        finally:
            release.set()
        await applying
        refresh_task = app._session_controls_refresh_task
        assert refresh_task is not None
        await refresh_task
        assert app.query_one(ActivityIndicator)._session_control_labels == ("Control",)


@pytest.mark.asyncio
async def test_session_control_refresh_coalesces_requests_and_discards_stale_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    first = application.runtime.sessions.create(title="First controls").session
    second = application.runtime.sessions.create(title="Second controls").session
    app = OuroTuiApp(application=application)
    app._projection = TuiSessionProjection(first.session_id)
    app._controller._session_id = first.session_id
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_statuses() -> tuple[TuiSessionControlStatus, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return (TuiSessionControlStatus(controller_id="stale", label="Stale"),)
        return (TuiSessionControlStatus(controller_id="fresh", label="Fresh"),)

    monkeypatch.setattr(app._controller, "session_control_statuses", delayed_statuses)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        app._schedule_session_controls_refresh()
        await asyncio.wait_for(entered.wait(), timeout=1)
        app._schedule_session_controls_refresh()
        app._schedule_session_controls_refresh()
        task = app._session_controls_refresh_task
        assert task is not None
        app._controller._session_id = second.session_id
        app._projection = TuiSessionProjection(second.session_id)
        release.set()
        await task

        assert calls == 2
        assert app.query_one(ActivityIndicator)._session_control_labels == ("Fresh",)


@pytest.mark.asyncio
async def test_session_picker_hides_route_identity_and_truncates_long_titles(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    long_title = "这是一个非常长的会话标题，用于验证选择菜单只保留可识别标题并在超出宽度时省略显示"
    application.runtime.sessions.create(
        session_id="ses_private_identity",
        title=long_title,
        title_source="user",
        current_model="private-provider/private-endpoint",
    )
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        group_header = options.get_option_at_index(0)
        assert group_header.disabled is True
        assert group_header.prompt.plain == "Today"
        assert options.highlighted == 1
        session_choice = next(
            choice for choice in app.screen._choices if choice.value == "ses_private_identity"
        )
        assert "ses_private_identity" not in session_choice.label
        assert "private-provider/private-endpoint" not in session_choice.label
        assert session_choice.label != long_title
        assert "…" in session_choice.label
        assert session_choice.description == ""
        assert session_choice.trailing == "idle"
        assert session_choice.marker == ""


@pytest.mark.asyncio
async def test_session_picker_marks_the_session_open_on_the_conversation_page(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    current = application.runtime.sessions.create(
        session_id="ses_current",
        title="Current session",
    ).session
    other = application.runtime.sessions.create(
        session_id="ses_other",
        title="Other session",
    ).session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=current.session_id,
            role="user",
            text="Current conversation",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=current.session_id,
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await _wait_for_active_session(app, pilot, current.session_id)
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")

        choices = {choice.value: choice for choice in app.screen._choices}
        assert choices[current.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices[other.session_id].marker == ""


@pytest.mark.asyncio
async def test_session_picker_reads_only_the_catalog_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    sessions = tuple(
        application.runtime.sessions.create(
            session_id=f"ses_index_only_{index}",
            title=f"Indexed {index}",
        ).session
        for index in range(3)
    )
    children = tuple(
        SessionRecord(
            session_id=f"ses_index_child_{index}",
            title=f"Indexed child {index}",
            namespace="task" if index == 1 else "default",
            session_kind="subagent",
            root_session_id=sessions[index % 2].session_id,
            parent_session_id=("ses_index_child_0" if index == 2 else sessions[index].session_id),
            delegation_id=f"dlg_index_child_{index}",
        )
        for index in range(3)
    )
    for child in children:
        application.runtime.store.create_session(child)
        assert application.runtime.store.refresh_catalog(child.session_id) is None
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()

        def unexpected_canonical_read(*_args: object) -> None:
            raise AssertionError("Ctrl+O must not load canonical Session records")

        with monkeypatch.context() as catalog_guard:
            catalog_guard.setattr(
                application.runtime.store,
                "load_session_record",
                unexpected_canonical_read,
            )
            catalog_guard.setattr(
                application.runtime.store,
                "list_sessions",
                unexpected_canonical_read,
            )
            await pilot.press("ctrl+o")
            await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
            assert {choice.value for choice in app.screen._choices} == {
                session.session_id for session in sessions
            }
            await pilot.press("tab")
            await _wait_for_screen(app, ChoiceScreen, title="SESSION • TASK")
            assert not app.screen._choices
            await pilot.press("tab")
            await _wait_for_screen(app, ChoiceScreen, title="SESSION • SUBAGENT")
            assert {choice.value for choice in app.screen._choices} == {
                child.session_id for child in children
            }


@pytest.mark.asyncio
async def test_session_picker_opens_canonical_session_when_catalog_entry_is_invalid(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(
        session_id="ses_invalid_index",
        title="Canonical session",
    ).session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="Canonical history remains readable",
            sequence=1,
        )
    )
    index_path = application.runtime.store._catalog.index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    indexed = next(item for item in payload["sessions"] if item["session_id"] == session.session_id)
    indexed.pop("session_kind")
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        choice = next(item for item in app.screen._choices if item.value == session.session_id)

        assert choice.trailing == "index error"
        assert choice.group == "Today"

        await pilot.press("enter")
        await _wait_for_active_session(app, pilot, session.session_id)

        assert app.query_one(ConversationHistory).user_turns == (
            "Canonical history remains readable",
        )
        assert not isinstance(app.screen, ChoiceScreen)


@pytest.mark.asyncio
async def test_fork_projects_animated_activity_until_the_branch_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(
        session_id="ses_fork_activity",
        title="Fork activity",
    ).session
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text="Fork this conversation",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=session.session_id,
    )

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await _wait_for_active_session(app, pilot, session.session_id)
        original_branch = app._controller.branch_session
        branch_started = asyncio.Event()
        release_branch = asyncio.Event()

        async def delayed_branch(message_id: str | None = None):
            branch_started.set()
            await release_branch.wait()
            return await original_branch(message_id)

        monkeypatch.setattr(app._controller, "branch_session", delayed_branch)
        app._fork_session("full")
        await asyncio.wait_for(branch_started.wait(), timeout=2)
        await pilot.pause()

        indicator = app.query_one(ActivityIndicator)
        assert indicator._activity == "forking"
        assert "Forking session" in indicator.render().plain
        assert indicator.render().plain.strip()[0] in DEFAULT_TUI_THEME.spinner_frames
        frame = indicator._frame
        indicator._tick()
        assert indicator._frame != frame

        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        assert indicator._activity == "forking"
        assert "Forking session" in indicator.render().plain
        menu_frame = indicator._frame
        await _wait_until(
            lambda: indicator._frame != menu_frame,
            pilot,
            description="fork indicator animation while the session picker is open",
        )

        release_branch.set()
        await _wait_for_worker_group(app, pilot, group="session-fork")
        assert app._controller.session_id != session.session_id
        assert not isinstance(app.screen, ChoiceScreen)
        assert "Ready" in indicator.render().plain

        branch_session_id = app._controller.session_id

        async def fail_branch(_message_id: str | None = None):
            raise PersistenceError("branch storage unavailable")

        monkeypatch.setattr(app._controller, "branch_session", fail_branch)
        app._fork_session("full")
        await _wait_for_worker_group(app, pilot, group="session-fork")

        assert app._controller.session_id == branch_session_id
        assert app._activity_override is None
        assert "Ready" in indicator.render().plain


@pytest.mark.asyncio
async def test_session_picker_contains_load_failure_in_choice_dialog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Damaged session").session
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()

        async def fail_load(_session_id: str) -> None:
            raise PersistenceError("message journal is invalid")

        monkeypatch.setattr(app._controller, "prepare_session_selection", fail_load)
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        session_menu = app.screen

        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen is not session_menu
        assert app.screen._title == "SESSION LOAD FAILED"
        error = app.screen._choices[0].description
        assert session.session_id in error
        assert "message journal is invalid" in error
        assert "Ready" in app.query_one(ActivityIndicator).render().plain

        await pilot.press("escape")
        await pilot.pause()
        assert session_menu not in app.screen_stack
        assert not isinstance(app.screen, ChoiceScreen)
        assert app.is_running is True


@pytest.mark.asyncio
async def test_session_picker_and_archive_menu_switch_and_toggle_archived_domain(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    active = application.runtime.sessions.create(
        session_id="ses_active",
        title="Active session",
    ).session
    archived = application.runtime.sessions.create(
        session_id="ses_archived",
        title="Archived session",
    ).session
    application.runtime.sessions.set_archived(archived.session_id, True)
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=active.session_id,
            role="user",
            text="Visible active session",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=active.session_id,
    )

    async with run_ready_app(app, size=(100, 34), notifications=True) as pilot:
        await _wait_for_active_session(app, pilot, active.session_id)
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        groups = {choice.value: choice.group for choice in app.screen._choices}
        assert groups[active.session_id] == "Today"
        assert archived.session_id not in groups
        await pilot.press("escape")

        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Archive session", title="ARCHIVE SESSIONS • USER")
        archive_screen = app.screen
        initial = {choice.value: choice for choice in archive_screen._choices}
        assert initial[active.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert archived.session_id not in initial

        archive_screen.query_one("#choice-filter", Input).value = "Active session"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is archive_screen
        await _wait_until(
            lambda: not archive_screen._choices,
            pilot,
            description="archived session removed from user domain",
        )
        await pilot.press("tab", "tab", "tab")
        await _wait_for_screen(app, ChoiceScreen, title="UNARCHIVE SESSIONS • ARCHIVED")
        choices = {choice.value: choice for choice in archive_screen._choices}
        assert set(choices) == {active.session_id, archived.session_id}
        assert {choice.group for choice in choices.values()} == {"Today"}
        assert choices[active.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert "Enter unarchive" in archive_screen.query_one("#choice-hint", Static).render().plain

        archive_screen.query_one("#choice-filter", Input).value = "Archived session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_until(
            lambda: archived.session_id not in {choice.value for choice in archive_screen._choices},
            pilot,
            description="restored session removed from archived domain",
        )
        assert app.screen is archive_screen
        await pilot.press("tab")
        await _wait_for_screen(app, ChoiceScreen, title="ARCHIVE SESSIONS • USER")
        assert [choice.value for choice in archive_screen._choices] == [archived.session_id]
        catalog = {item.session_id: item for item in application.runtime.sessions.catalog().entries}
        assert catalog[active.session_id].archived is True
        assert catalog[archived.session_id].archived is False
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_rename_session_menu_edits_active_session_inline_and_stays_open(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    active = application.runtime.sessions.create(
        session_id="ses_rename_active",
        title="Original title",
    ).session
    archived = application.runtime.sessions.create(
        session_id="ses_rename_archived",
        title="Archived title",
    ).session
    application.runtime.sessions.set_archived(archived.session_id, True)
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=active.session_id,
            role="user",
            text="Visible rename session",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=active.session_id,
    )

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await _wait_for_active_session(app, pilot, active.session_id)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        app.screen.query_one("#choice-filter", Input).value = "Rename session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, EditableChoiceScreen, title="RENAME SESSION • USER")

        rename_screen = app.screen
        assert isinstance(rename_screen, EditableChoiceScreen)
        assert action_screen.query_one("#choice-panel", Vertical).display is False
        assert [choice.value for choice in rename_screen._choices] == [active.session_id]

        rename_screen.query_one("#choice-filter", Input).value = "Original title"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is rename_screen
        assert rename_screen._editing_value == active.session_id
        assert rename_screen.query_one("#choice-filter", Input).disabled is True

        await pilot.press(*(("backspace",) * len(active.title)), "enter")
        await pilot.pause()
        assert app.screen is rename_screen
        assert rename_screen._editing_value == active.session_id
        assert application.runtime.sessions.open(active.session_id).session.title == active.title

        await pilot.press(
            *tuple("Renamed"),
            "space",
            *tuple("title"),
            "enter",
        )
        await pilot.pause()
        assert app.screen is rename_screen
        await _wait_until(
            lambda: (
                rename_screen._editing_value is None
                and rename_screen.query_one("#choice-filter", Input).has_focus
            ),
            pilot,
            description="persisted session rename and restored input focus",
        )
        assert rename_screen.query_one("#choice-filter", Input).disabled is False
        assert rename_screen.query_one("#choice-filter", Input).has_focus is True
        assert rename_screen.query_one("#choice-filter", Input).value == ""
        assert rename_screen._choices[0].edit_value == "Renamed title"
        assert application.runtime.sessions.open(active.session_id).session.title == (
            "Renamed title"
        )
        assert app._active_title == "Renamed title"
        await pilot.press("enter", "home", "x", "escape")
        await pilot.pause()
        assert app.screen is rename_screen
        assert rename_screen._editing_value is None
        assert application.runtime.sessions.open(active.session_id).session.title == (
            "Renamed title"
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is action_screen
        assert action_screen.query_one("#choice-panel", Vertical).display is True


@pytest.mark.asyncio
async def test_delete_session_menu_force_deletes_catalog_error_and_quits_cleanly(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(
        session_id="ses_delete_catalog_error",
        title="Readable canonical session",
    ).session
    application.runtime.sessions.set_archived(session.session_id, True)
    index_path = application.runtime.store._catalog.index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    indexed = next(item for item in payload["sessions"] if item["session_id"] == session.session_id)
    indexed.pop("session_kind")
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    session_dir = application.runtime.store.sessions_dir / session.session_id
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Delete session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, MultiChoiceScreen, title="DELETE SESSIONS • ARCHIVED")

        choice = next(item for item in app.screen._choices if item.value == session.session_id)
        assert choice.trailing == "index error"

        await pilot.press("enter")
        confirm = app.screen.query_one(OptionList).get_option("__multi_confirm__").prompt
        assert isinstance(confirm, Text)
        assert "Permanently delete" in confirm.plain
        await pilot.press("down", "enter")
        await _wait_until(
            lambda: not app._sessions,
            pilot,
            description="deleted Session catalog projection",
        )

        assert not session_dir.exists()
        assert application.runtime.sessions.catalog().entries == ()
        assert not list(app.query("Toast"))

        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
async def test_delete_session_menu_keeps_current_marker_separate_from_multi_selection(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    current = application.runtime.sessions.create(
        session_id="ses_delete_current",
        title="Current archived session",
    ).session
    other = application.runtime.sessions.create(
        session_id="ses_delete_other",
        title="Other archived session",
    ).session
    application.runtime.sessions.set_archived(current.session_id, True)
    application.runtime.sessions.set_archived(other.session_id, True)
    application.runtime.store.append_message(
        MessageRecord.text(
            session_id=current.session_id,
            role="user",
            text="Visible archived session",
            sequence=1,
        )
    )
    app = OuroTuiApp(
        application=application,
        resume_session_id=current.session_id,
    )

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await _wait_for_active_session(app, pilot, current.session_id)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Delete session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, MultiChoiceScreen, title="DELETE SESSIONS • ARCHIVED")

        delete_screen = app.screen
        choices = {choice.value: choice for choice in delete_screen._choices}
        assert choices[current.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert choices[other.session_id].marker == ""
        assert (
            choices[current.session_id].selection_marker
            == DEFAULT_TUI_THEME.unselected_choice_marker
        )
        assert (
            choices[other.session_id].selection_marker == DEFAULT_TUI_THEME.unselected_choice_marker
        )

        options = delete_screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index(other.session_id)
        options.focus()
        await pilot.press("space")
        await pilot.pause()

        choices = {choice.value: choice for choice in delete_screen._choices}
        assert choices[current.session_id].marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert (
            choices[current.session_id].selection_marker
            == DEFAULT_TUI_THEME.unselected_choice_marker
        )
        assert (
            choices[other.session_id].selection_marker == DEFAULT_TUI_THEME.selected_choice_marker
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_session_settings_can_be_completed_with_keyboard_only(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"approval", "enter", *"afk", "enter")
        await pilot.pause()
        assert await app._controller.session_approval_preset() == "afk"

        await pilot.press("slash", *"thinking", "enter", *"high", "enter")
        await pilot.pause()
        reasoning = await app._controller.session_reasoning()
        assert reasoning.requested.effort == "high"
        assert reasoning.effective.effort == "high"


@pytest.mark.asyncio
async def test_session_setting_shortcuts_cycle_and_wrap_available_values(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_thinking_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()

        for expected in (
            ReasoningRequest(mode="enabled", effort="high"),
            ReasoningRequest(mode="disabled"),
            ReasoningRequest(mode="enabled", effort="low"),
        ):
            await pilot.press("alt+t")
            await pilot.pause()
            assert (await app._controller.session_reasoning()).requested == expected

        for expected in ("auto", "approval", "deny", "afk", "inherit"):
            await pilot.press("alt+a")
            await pilot.pause()
            assert await app._controller.session_approval_preset() == expected

        assert app.query_one(Composer).text == ""


@pytest.mark.asyncio
async def test_actions_and_session_shortcuts_switch_on_the_active_menu_surface(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    application.runtime.sessions.create(session_id="ses_shortcut", title="Shortcut session")
    app = OuroTuiApp(home=tmp_path, application=application)

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        for key, title, page_depth in (
            ("ctrl+o", "SESSION • USER", 1),
            ("ctrl+s", "SKILLS", 1),
            ("ctrl+s", "SKILLS", 1),
            ("ctrl+p", "ACTIONS", 0),
            ("ctrl+p", "ACTIONS", 0),
        ):
            await pilot.press(key)
            await _wait_for_screen(app, ChoiceScreen, title=title)
            assert app.screen is menu
            assert len(menu._page_stack) == page_depth
            assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1

        await pilot.press("escape")


@pytest.mark.asyncio
async def test_action_menu_executes_new_session_and_help_shortcuts_in_context(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        await pilot.press("f1")
        await _wait_for_screen(app, ChoiceScreen, title="KEYBOARD HELP")
        assert app.screen is menu
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1
        assert menu.query_one("#choice-panel", Vertical).display is True
        assert menu.query_one("#choice-hint", Static).render().plain == (
            app.tui_theme.inline_separator.join(("↑/↓ move", "←/→ group", "Esc back"))
        )
        assert {choice.group for choice in menu._choices} == {
            "SHORTCUTS",
            "COMMANDS",
            "PLUGIN COMMANDS",
        }
        shortcuts = {
            choice.label: choice.trailing for choice in menu._choices if choice.group == "SHORTCUTS"
        }
        assert shortcuts["Alt+T"] == "cycle model thinking"
        assert shortcuts["Alt+A"] == "cycle tool approval"
        assert shortcuts["Tab"] == "focus / switch menu domain"
        assert shortcuts["Alt+← / Alt+→"] == "previous / next agent"
        assert shortcuts["Alt+↑ / Alt+↓"] == "main / last agent"
        assert shortcuts["Ctrl+Enter"] == "new line"
        assert shortcuts["Ctrl+G"] == "guide current task"
        assert shortcuts["Ctrl+S"] == "skills"
        files = next(choice for choice in menu._choices if choice.value == "command:@file")
        assert (files.group, files.label, files.trailing) == (
            "COMMANDS",
            "@file",
            "Reference files",
        )
        assert files.description == (
            "Type to filter; Space selects; Enter opens folders or adds references; Esc closes."
        )
        plugin_commands = tuple(
            choice for choice in menu._choices if choice.group == "PLUGIN COMMANDS"
        )
        goal = next(choice for choice in plugin_commands if choice.label.startswith("/goal "))
        plan = next(choice for choice in plugin_commands if choice.label == "/plan [on|off|status]")
        assert goal.trailing == "Create and control the latest Goal for this conversation."
        assert plan.trailing == "Enter plan mode for the current Session."

        await pilot.press("f1")
        await pilot.pause()
        assert app.screen is menu
        assert menu.navigation_id == "help"
        assert len(menu._page_stack) == 1

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is menu
        assert menu.query_one("#choice-hint", Static).render().plain == (ChoiceScreen.DEFAULT_HINT)

        await pilot.press("ctrl+n")
        await _wait_for_worker_group(app, pilot, group="navigation")
        assert not isinstance(app.screen, ChoiceScreen)
        assert len(app.screen_stack) == 1
        assert app._active_title == "New conversation"
