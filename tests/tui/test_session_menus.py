from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList, Static

from ouro.core.errors import PersistenceError
from ouro.core.state import SessionRecord
from ouro.tui.app import OuroTuiApp, _session_choices
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.screens import ChoiceScreen, EditableChoiceScreen, MultiChoiceScreen
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import (
    _application,
    _persist_direct_child_session,
    _wait_for_screen,
    _wait_until,
)


def test_workspace_groups_preserve_catalog_order_and_distinguish_full_paths(tmp_path: Path) -> None:
    application = _application(tmp_path)
    application.runtime.sessions.create(title="Template")
    template = application.runtime.sessions.catalog().entries[0]
    workspaces = ("/a/project", "/b/project", "/a/project", None, "/b/project")
    sessions = tuple(
        template.model_copy(
            update={
                "session_id": f"ses_{index}",
                "workspace": workspace,
                "last_activity_at": datetime(2026, 10, 9, tzinfo=UTC) - timedelta(days=index),
                "archived": index % 2 == 0,
            }
        )
        for index, workspace in enumerate(workspaces)
    )
    choices = _session_choices(sessions, grouping="workspace", selected_session_id="ses_2")
    assert [(choice.value, choice.group) for choice in choices] == [
        ("ses_0", "/a/project"),
        ("ses_2", "/a/project"),
        ("ses_1", "/b/project"),
        ("ses_4", "/b/project"),
        ("ses_3", "No workspace"),
    ]
    assert [choice.value for choice in choices if choice.marker] == ["ses_2"]


@pytest.mark.parametrize("action", ["Open session", "Rename session", "Archive session"])
async def test_session_menus_share_domains_grouping_filter_and_navigation(
    tmp_path: Path, action: str
) -> None:
    application = _application(tmp_path)
    workspace = str(tmp_path / "project")
    user = application.runtime.sessions.create(title="User", workspace=workspace).session
    task = application.runtime.sessions.create(title="Task", namespace="task").session
    child = SessionRecord(
        session_id="ses_child",
        title="Child",
        session_kind="subagent",
        namespace="task",
        root_session_id=user.session_id,
        parent_session_id=user.session_id,
        delegation_id="dlg_child",
        workspace=workspace,
    )
    application.runtime.store.create_session(child)
    assert application.runtime.store.refresh_catalog(child.session_id) is None
    archived = application.runtime.sessions.create(title="Archived", workspace=workspace).session
    archived_task = application.runtime.sessions.create(
        title="Archived task", namespace="task"
    ).session
    for session in (archived, archived_task):
        application.runtime.sessions.set_archived(session.session_id, True)
    TuiPreferencesStore(tmp_path).save(TuiPreferences(session_grouping="workspace"))
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        parent = app.screen
        parent.query_one(Input).value = action
        await pilot.pause()
        await pilot.press("enter")
        screen = app.screen
        assert isinstance(screen, ChoiceScreen)
        for domain, expected in (
            ("USER", {user.session_id: workspace}),
            ("TASK", {task.session_id: "No workspace"}),
            ("SUBAGENT", {child.session_id: workspace}),
            (
                "ARCHIVED",
                {archived.session_id: workspace, archived_task.session_id: "No workspace"},
            ),
            ("USER", {user.session_id: workspace}),
        ):
            assert app.screen is screen
            assert screen.page_title.endswith(f" • {domain}")
            assert {choice.value: choice.group for choice in screen._choices} == expected
            assert "Tab switch sessions" in screen.query_one("#choice-hint", Static).render().plain
            if domain == "ARCHIVED":
                screen.query_one(Input).value = "Archived task"
                await pilot.pause()
                options = screen.query_one(OptionList)
                assert (
                    options.get_option_at_index(options.highlighted).id == archived_task.session_id
                )
            await pilot.press("tab")
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is parent
        assert parent.query_one(Input).value == action
        await pilot.press("tab")
        assert parent.page_title == "ACTIONS"


async def test_rename_archived_and_subagent_sessions_preserves_editor_context(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    root = application.runtime.sessions.create(title="Root").session
    child = SessionRecord(
        session_id="ses_rename_child",
        title="Child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_rename_child",
    )
    application.runtime.store.create_session(child)
    application.runtime.store.refresh_catalog(child.session_id)
    application.runtime.sessions.set_archived(root.session_id, True)
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        app.screen.query_one(Input).value = "Rename session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, EditableChoiceScreen, title="RENAME SESSION • USER")
        screen = app.screen
        assert not screen._choices
        await pilot.press("tab", "tab")
        for session_id, domain in ((child.session_id, "SUBAGENT"), (root.session_id, "ARCHIVED")):
            await pilot.press("enter", "ctrl+u", "n", "e", "w", "tab")
            assert screen.page_title == f"RENAME SESSION • {domain}"
            assert screen._editing_value == session_id
            await pilot.press("enter")
            await _wait_until(
                lambda: screen._editing_value is None, pilot, description="saved session title"
            )
            assert application.runtime.sessions.open(session_id).session.title == "new"
            await pilot.press("tab")
        assert application.runtime.sessions.open(root.session_id).session.archived


@pytest.mark.parametrize("fail_save", [False, True])
async def test_session_grouping_setting_persists_only_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_save: bool
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        app.screen.query_one(Input).value = "Session grouping"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION GROUPING")
        screen = app.screen
        assert [choice.value for choice in screen._choices if choice.marker] == ["date"]
        if fail_save:

            def failed_save(_preferences: TuiPreferences) -> None:
                raise PersistenceError("Preference write failed")

            monkeypatch.setattr(app._preferences_store, "save", failed_save)
        await pilot.press("down", "enter")
        await pilot.pause()
        expected = "date" if fail_save else "workspace"
        assert app._preferences.session_grouping == expected
        assert TuiPreferencesStore(tmp_path).load().session_grouping == expected
        assert [choice.value for choice in screen._choices if choice.marker] == [expected]
        await pilot.press("escape", "escape")
    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    assert restarted._preferences.session_grouping == expected


@pytest.mark.parametrize("empty", [False, True])
async def test_delete_menu_is_archived_only_and_rechecks_before_confirmation(
    tmp_path: Path, empty: bool
) -> None:
    application = _application(tmp_path)
    active = application.runtime.sessions.create(title="Active").session
    archived = application.runtime.sessions.create(title="Archived", namespace="task").session
    if not empty:
        application.runtime.sessions.set_archived(archived.session_id, True)
    # Unreadable, unarchived sessions must not become a deletion exception.
    (tmp_path / "sessions" / active.session_id / "session.json").write_text(
        "broken", encoding="utf-8"
    )
    application.runtime.store.refresh_catalog(active.session_id)
    TuiPreferencesStore(tmp_path).save(TuiPreferences(session_grouping="workspace"))
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        app.screen.query_one(Input).value = "Delete session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, MultiChoiceScreen, title="DELETE SESSIONS • ARCHIVED")
        screen = app.screen
        assert [choice.value for choice in screen._choices] == (
            [] if empty else [archived.session_id]
        )
        assert {choice.group for choice in screen._choices} == (
            set() if empty else {"No workspace"}
        )
        assert "Tab" not in screen.query_one("#choice-hint", Static).render().plain
        assert "Esc back" in screen.query_one("#choice-hint", Static).render().plain
        await pilot.press("tab")
        assert screen.page_title == "DELETE SESSIONS • ARCHIVED"
        if not empty:
            await pilot.press("enter")
            application.runtime.sessions.set_archived(archived.session_id, False)
            await pilot.press("down", "enter")
            await _wait_until(
                lambda: not screen._choices, pilot, description="refreshed archived sessions"
            )
            assert application.runtime.sessions.open(archived.session_id).session.archived is False
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")


async def test_archive_open_and_delete_subagent_uses_existing_navigation(tmp_path: Path) -> None:
    application = _application(tmp_path)
    root_id, child_id = _persist_direct_child_session(application)
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        app.screen.query_one(Input).value = "Archive session"
        await pilot.pause()
        await pilot.press("enter", "tab", "tab")
        await _wait_for_screen(app, ChoiceScreen, title="ARCHIVE SESSIONS • SUBAGENT")
        await pilot.press("enter")
        await _wait_until(lambda: not app.screen._choices, pilot, description="archived subagent")
        await pilot.press("tab")
        await _wait_for_screen(app, ChoiceScreen, title="UNARCHIVE SESSIONS • ARCHIVED")
        assert [choice.value for choice in app.screen._choices] == [child_id]
        await pilot.press("ctrl+o")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • ARCHIVED")
        await pilot.press("enter")
        await _wait_until(
            lambda: app._visible_session_id() == child_id,
            pilot,
            description="opened archived subagent",
        )
        assert app._visible_read_only
        assert app._controller.session_id == root_id
        await pilot.press("ctrl+n")
        await _wait_until(
            lambda: app._visible_session_id() is None,
            pilot,
            description="returned from read-only subagent to welcome",
        )
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one(Input).value = "Delete session"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, MultiChoiceScreen, title="DELETE SESSIONS • ARCHIVED")
        await pilot.press("enter", "down", "enter")
        await _wait_until(
            lambda: app._visible_session_id() is None and not app.screen._choices,
            pilot,
            description="deleted archived subagent",
        )
        assert not app._visible_read_only
        assert {entry.session_id for entry in application.runtime.sessions.catalog().entries} == {
            root_id
        }
