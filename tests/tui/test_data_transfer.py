from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Input, OptionList

from ouro.application.data_transfer import DataTransferRequest
from ouro.cli.main import main
from ouro.config.prompts import PromptConfigManager, PromptDraft
from ouro.core.harness import HarnessState
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import SessionControlActivation, SessionControlState
from ouro.core.state import SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.local_application import build_local_data_transfer_service, build_local_session_services
from ouro.transfer.archive import DataTransferError, read_archive
from ouro.tui.app import OuroTuiApp
from ouro.tui.archive_paths import ArchivePathScreen
from ouro.tui.commands import resolve_command
from ouro.tui.data_transfer import DataTransferScreen
from ouro.tui.screens import ChoiceScreen, MultiChoiceScreen
from ouro.tui.widgets import Composer, ConversationHistory
from tests.tui.helpers import wait_for_screen, wait_until


def test_transfer_command_only_exports_current_session_without_arguments():
    command, argument = resolve_command("/export")
    assert command is not None and command.name == "/export"
    assert not argument
    assert not command.takes_arguments
    assert not command.input_hint
    assert resolve_command("/import")[0] is None
    assert resolve_command("/data")[0] is None


@pytest.mark.asyncio
async def test_tui_data_form_and_command_use_application_service(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = OuroTuiApp(home=tmp_path / "home")
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "export", "providers")
        async with asyncio.timeout(5):
            while not isinstance(app.screen, DataTransferScreen):
                await pilot.pause()
        screen = app.screen
        assert {choice.value for choice in screen._choices} == {
            "kind",
            "path",
            "replace",
            "submit",
        }
        archive = screen._path
        assert archive is not None and archive.parent == tmp_path
        await screen._select_choice("submit")
        await app.workers.wait_for_complete()
        assert read_archive(archive, "providers").manifest.kind == "providers"
        # The same archive goes through the CLI import boundary and TUI import service.
        assert (
            main(
                [
                    "data",
                    "import",
                    "--kind",
                    "providers",
                    "--path",
                    str(archive),
                    "--home",
                    str(tmp_path / "other"),
                    "--json",
                ]
            )
            == 0
        )
        capsys.readouterr()
        result = await app._controller.transfer_data(
            DataTransferRequest(
                action="import",
                kind="providers",
                path=archive,
                replace=True,
            )
        )
        assert result.kind == "providers"
        assert result.restart_required
        assert "__ouro_system_setting__:data.transfer" in {
            choice.value for choice in app._action_choices()
        }
        assert not {"/import", "/export"}.intersection(
            choice.value for choice in app._action_choices()
        )


@pytest.mark.asyncio
async def test_imported_history_opens_with_missing_dependencies_in_live_sqlite_host(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    store = build_local_session_services(source)._store
    session = SessionRecord(current_model="missing-provider/missing-model")
    store.create_session(session)
    activation = SessionControlActivation(
        controller_id="dev.missing/session_controllers/mode",
        plugin_id="dev.missing",
        controller_local_id="mode",
        plugin_class="user",
        release_id="missing-release",
        generation_id="missing-generation",
        execution_contract_hash="missing-contract",
    )
    store.commit_session(
        SessionCommit(
            before=session,
            steps=(
                SessionCommitStep(
                    message=MessageRecord(
                        session_id=session.session_id,
                        role="assistant",
                        tool_calls=[ToolCallRequest(name="missing_tool", arguments={})],
                    ),
                    harness=HarnessState(controls=SessionControlState(activations=[activation])),
                ),
            ),
        )
    )
    before = store.load_session(session.session_id)
    archive = tmp_path / "sessions.zip"
    build_local_data_transfer_service(source).transfer(
        DataTransferRequest(action="export", kind="sessions", path=archive)
    )
    app = OuroTuiApp(home=target, session_backend="sqlite")
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        result = await app._controller.transfer_data(
            DataTransferRequest(action="import", kind="sessions", path=archive)
        )
        assert result.count == 1
        loaded = await app._controller.load_session(session.session_id)
        assert loaded.messages == tuple(before.messages)
        assert loaded.session_controls[0].controller_id == activation.controller_id
        async with asyncio.timeout(10):
            await app._open_session(session.session_id)
        assert app._visible_session_id() == session.session_id
        assert app._active_model == "Unavailable model"
        assert app._controller._runtime().store.load_session(session.session_id) == before
        assert not (target / "sessions" / session.session_id).exists()


async def _choose_menu_row(app, pilot, identity):
    # A background scan can leave the previous menu visible until the picker
    # is prepared. Wait for the requested row on the currently active screen.
    async with asyncio.timeout(10):
        while True:
            menu = app.screen
            menu.query_one("#choice-filter", Input).value = ""
            await pilot.pause()
            if app.screen is not menu:
                continue
            options = menu.query_one("#choice-list", OptionList)
            if any(
                options.get_option_at_index(index).id == identity
                for index in range(options.option_count)
            ):
                break
    options.highlighted = options.get_option_index(identity)
    await pilot.press("enter")
    await pilot.pause()


async def _open_data_menu(app, pilot, action, kind=None):
    await app._choose_action()
    await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
    menu = app.screen
    await _choose_menu_row(app, pilot, "__ouro_system_setting__:data.transfer")
    await wait_for_screen(app, ChoiceScreen, title="MANAGE DATA")
    assert app.screen is menu and menu._title == "MANAGE DATA"
    await _choose_menu_row(app, pilot, action)
    if action == "import":
        await wait_for_screen(app, DataTransferScreen, title="IMPORT DATA")
        assert isinstance(app.screen, DataTransferScreen)
        assert app.screen._kind is None
        assert "kind" not in {row.value for row in app.screen._choices}
        return
    await wait_for_screen(app, ChoiceScreen, title="EXPORT DATA")
    assert app.screen is menu
    assert {row.value for row in menu._choices} == {
        "providers",
        "provider-templates",
        "mcp",
        "agents",
        "prompts",
        "sessions",
        "skills",
    }
    assert next(row for row in menu._choices if row.value == "prompts").label == (
        "Personal prompt configuration"
    )
    await _choose_menu_row(app, pilot, kind)
    await wait_for_screen(app, DataTransferScreen, title="EXPORT DATA")
    assert isinstance(app.screen, DataTransferScreen)


@pytest.mark.asyncio
async def test_tui_exports_personal_prompt_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    PromptConfigManager(home).save_entry(
        "addition",
        PromptDraft(description="Portable", text="Keep answers concise", enabled=True),
        expected_revision=0,
    )
    app = OuroTuiApp(home=home)
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "export", "prompts")
        form = app.screen
        assert isinstance(form, DataTransferScreen)
        assert next(row for row in form._choices if row.value == "kind").trailing == (
            "Personal prompt configuration"
        )
        await _choose_menu_row(app, pilot, "submit")
        await app.workers.wait_for_complete()
        assert form._path is not None
        payload = read_archive(form._path, "prompts").manifest.data
        assert payload["prompts"]["additions"][0]["text"] == "Keep answers concise"
        assert not app._data_transfer_progress.result.restart_required


@pytest.mark.asyncio
@pytest.mark.parametrize("select_all", [False, True])
async def test_session_import_selection_uses_archive_titles_and_file_browser(
    tmp_path, monkeypatch, select_all
):
    monkeypatch.chdir(tmp_path)
    source = build_local_session_services(tmp_path / "source")
    source.sessions.create(session_id="ses_alpha", title="Alpha conversation")
    source.sessions.create(session_id="ses_beta", title="Beta conversation")
    source.sessions.create(session_id="ses_gamma", title="Gamma conversation")
    archive = tmp_path / "all sessions.zip"
    build_local_data_transfer_service(tmp_path / "source").transfer(
        DataTransferRequest(action="export", kind="sessions", path=archive)
    )
    target = tmp_path / "target"
    app = OuroTuiApp(home=target)
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "import")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, DataTransferScreen)
        assert form._kind is None
        assert "selection" not in {row.value for row in form._choices}
        assert next(row for row in form._choices if row.value == "submit").disabled
        await _choose_menu_row(app, pilot, "path")
        assert isinstance(app.screen, ArchivePathScreen)
        await _choose_menu_row(app, pilot, "file:all sessions.zip")
        assert app.screen is form
        assert form._path == archive
        await app.workers.wait_for_complete()
        assert form._kind == "sessions"
        await _choose_menu_row(app, pilot, "selection")
        assert isinstance(app.screen, ChoiceScreen)
        if select_all:
            await _choose_menu_row(app, pilot, "all")
            assert form._identities == ()
        else:
            await _choose_menu_row(app, pilot, "selected")
            assert isinstance(app.screen, MultiChoiceScreen)
            assert {row.label for row in app.screen._choices} == {
                "Beta conversation",
                "Alpha conversation",
                "Gamma conversation",
            }
            options = app.screen.query_one("#choice-list", OptionList)
            for identity in ("ses_alpha", "ses_beta"):
                options.highlighted = options.get_option_index(identity)
                await pilot.press("space")
            await pilot.press("enter")
            await _choose_menu_row(app, pilot, "__multi_confirm__")
            assert set(form._identities) == {"ses_alpha", "ses_beta"}
            await _choose_menu_row(app, pilot, "selection")
            await _choose_menu_row(app, pilot, "selected")
            assert app.screen._selected == {"ses_alpha", "ses_beta"}
            await pilot.press("escape")
            await pilot.pause()
        assert app.screen is form
        assert next(row for row in form._choices if row.value == "replace").trailing == "Skip"
        await _choose_menu_row(app, pilot, "submit")
        await app.workers.wait_for_complete()
        assert {
            record.session_id
            for record in build_local_session_services(target)._store.list_sessions()
        } == ({"ses_alpha", "ses_beta", "ses_gamma"} if select_all else {"ses_alpha", "ses_beta"})


@pytest.mark.asyncio
async def test_export_picker_uses_default_filename_and_cancel_keeps_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backups").mkdir()
    app = OuroTuiApp(home=tmp_path / "home")
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "export", "providers")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, DataTransferScreen)
        original = form._path
        await _choose_menu_row(app, pilot, "path")
        await _choose_menu_row(app, pilot, "directory:backups")
        await pilot.press("escape")
        await pilot.pause()
        assert form._path == original
        await _choose_menu_row(app, pilot, "path")
        await _choose_menu_row(app, pilot, "directory:backups")
        await _choose_menu_row(app, pilot, "save")
        assert form._path == tmp_path / "backups" / original.name
        await _choose_menu_row(app, pilot, "submit")
        await app.workers.wait_for_complete()
        assert read_archive(form._path, "providers").manifest.kind == "providers"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first", [False, True])
async def test_transfer_progress_stays_in_menu_and_outlives_picker(
    tmp_path, monkeypatch, fail_first
):
    monkeypatch.chdir(tmp_path)
    app = OuroTuiApp(home=tmp_path / "home")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    notices = []
    original = app._controller.transfer_data

    async def delayed(request):
        calls.append(request)
        entered.set()
        await release.wait()
        if fail_first and len(calls) == 1:
            raise DataTransferError("Archive cannot be written")
        return await original(request)

    monkeypatch.setattr(app._controller, "transfer_data", delayed)
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        monkeypatch.setattr(
            app, "notify", lambda message, **kwargs: notices.append((message, kwargs))
        )
        history = app.query_one(ConversationHistory)
        before = [(entry.kind, entry.text) for entry in history.entries]
        await _open_data_menu(app, pilot, "export", "providers")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, DataTransferScreen)
        try:
            await _choose_menu_row(app, pilot, "submit")
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert app.screen is form
            assert any(row.loading for row in form._choices)
            assert next(row for row in form._choices if row.value == "submit").disabled
            app._request_data_transfer(form._request())
            assert len(calls) == 1
            await _choose_menu_row(app, pilot, "replace")
            assert form._replace
            assert not calls[0].replace
            await pilot.press("escape", "escape", "escape", "escape")
            await pilot.pause()
            await pilot.press(*"draft")
            assert app.query_one(Composer).text == "draft"
            await _open_data_menu(app, pilot, "export", "providers")
            await pilot.pause()
            reopened = app.screen
            assert isinstance(reopened, DataTransferScreen) and reopened is not form
            assert any(row.loading for row in reopened._choices)
        finally:
            release.set()
        await app.workers.wait_for_complete()
        assert app.screen is reopened
        assert not any(row.loading for row in reopened._choices)
        if fail_first:
            assert any(row.label == "Export failed" for row in reopened._choices)
            assert notices[-1][1]["title"] == "Export failed"
            assert notices[-1][1]["severity"] == "error"
            await _choose_menu_row(app, pilot, "submit")
            await app.workers.wait_for_complete()
            assert len(calls) == 2
        assert any(row.value == "result:0" for row in reopened._choices)
        assert len(notices) == (2 if fail_first else 1)
        assert notices[-1][1]["title"] == "Export completed"
        assert notices[-1][1]["markup"] is False
        assert "Exported providers" in notices[-1][0]
        assert [(entry.kind, entry.text) for entry in history.entries] == before


@pytest.mark.asyncio
async def test_export_command_pins_current_session_and_rejects_empty_or_arguments(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    services = build_local_session_services(home)
    services.sessions.create(session_id="ses_current", title="Current")
    services.sessions.create(session_id="ses_other", title="Other")
    app = OuroTuiApp(home=home)
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        welcome = app.screen
        await app._dispatch_command("/export")
        await pilot.pause()
        assert app.screen is welcome
        assert app._data_transfer_progress is None
        await app._open_session("ses_current")
        await app._dispatch_command("/export sessions")
        assert app.screen is welcome
        await pilot.press("slash", *"export", "enter")
        await pilot.pause()
        form = app.screen
        assert isinstance(form, DataTransferScreen)
        assert form._request().identities == ("ses_current",)
        assert form._request().kind == "sessions"
        assert "selection" not in {row.value for row in form._choices}
        assert next(row for row in form._choices if row.value == "kind").disabled
        await _choose_menu_row(app, pilot, "submit")
        async with asyncio.timeout(10):
            while app._data_transfer_progress.running:
                await pilot.pause()
        assert app._data_transfer_progress.result.count == 1
        target = build_local_data_transfer_service(tmp_path / "target")
        target.transfer(DataTransferRequest(action="import", kind="sessions", path=form._path))
        records = build_local_session_services(tmp_path / "target")._store.list_sessions()
        assert [record.session_id for record in records] == ["ses_current"]


@pytest.mark.asyncio
async def test_data_menu_returns_through_pages_without_changing_conversation(tmp_path):
    app = OuroTuiApp(home=tmp_path / "home")
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "export", "skills")
        await pilot.press("escape")
        await wait_for_screen(app, ChoiceScreen, title="EXPORT DATA")
        menu = app.screen
        assert menu._title == "EXPORT DATA"
        await pilot.press("escape")
        await wait_for_screen(app, ChoiceScreen, title="MANAGE DATA")
        assert app.screen is menu and menu._title == "MANAGE DATA"
        await _choose_menu_row(app, pilot, "import")
        await wait_for_screen(app, DataTransferScreen, title="IMPORT DATA")
        assert isinstance(app.screen, DataTransferScreen)
        assert app.screen._kind is None
        await pilot.press("escape")
        await wait_for_screen(app, ChoiceScreen, title="MANAGE DATA")
        await pilot.press("escape")
        await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is menu and menu._title == "ACTIONS"


@pytest.mark.asyncio
async def test_export_session_selection_displays_identity_as_trailing_label(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    services = build_local_session_services(home)
    services.sessions.create(session_id="ses_alpha", title="Conversation")
    services.sessions.create(session_id="ses_beta", title="Conversation")
    app = OuroTuiApp(home=home)
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "export", "sessions")
        form = app.screen
        await _choose_menu_row(app, pilot, "selection")
        await _choose_menu_row(app, pilot, "selected")
        await wait_for_screen(app, MultiChoiceScreen, title="SELECT SESSIONS")
        assert isinstance(app.screen, MultiChoiceScreen)
        assert {(row.label, row.trailing, row.description) for row in app.screen._choices} == {
            ("Conversation", "ses_alpha", ""),
            ("Conversation", "ses_beta", ""),
        }
        app.screen.query_one("#choice-filter", Input).value = "ses_beta"
        await pilot.pause()
        await pilot.press("space", "enter")
        await _choose_menu_row(app, pilot, "__multi_confirm__")
        await wait_until(
            lambda: form._identities == ("ses_beta",),
            pilot,
            description="export session selection applied",
        )
        assert form._identities == ("ses_beta",)
        await _choose_menu_row(app, pilot, "submit")
        await app.workers.wait_for_complete()
        assert app._data_transfer_progress.result.count == 1


@pytest.mark.asyncio
async def test_import_detects_file_type_resets_options_and_recovers_from_invalid_file(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source"
    services = build_local_session_services(source)
    services.sessions.create(session_id="ses_source", title="Source")
    transfer = build_local_data_transfer_service(source)
    config = tmp_path / "one.zip"
    sessions = tmp_path / "two.zip"
    invalid = tmp_path / "three.zip"
    transfer.transfer(DataTransferRequest(action="export", kind="providers", path=config))
    transfer.transfer(DataTransferRequest(action="export", kind="sessions", path=sessions))
    invalid.write_bytes(b"not an archive")
    app = OuroTuiApp(home=tmp_path / "target")
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "import")
        form = app.screen

        async def choose_file(path):
            await _choose_menu_row(app, pilot, "path")
            await _choose_menu_row(app, pilot, f"file:{path.name}")
            await app.workers.wait_for_complete()

        await choose_file(config)
        assert form._kind == "providers"
        await _choose_menu_row(app, pilot, "replace")
        assert form._replace
        await choose_file(sessions)
        assert form._kind == "sessions" and not form._replace
        await _choose_menu_row(app, pilot, "selection")
        await _choose_menu_row(app, pilot, "selected")
        await pilot.press("space", "enter")
        await _choose_menu_row(app, pilot, "__multi_confirm__")
        assert form._identities == ("ses_source",)
        await choose_file(invalid)
        assert form._kind is None and not form._identities
        assert any(row.value == "archive-status" and row.description for row in form._choices)
        assert next(row for row in form._choices if row.value == "submit").disabled
        assert app._data_transfer_progress.request is None
        invalid.write_bytes(config.read_bytes())
        await choose_file(invalid)
        assert form._kind == "providers" and form._archive_error is None
        await _choose_menu_row(app, pilot, "submit")
        await app.workers.wait_for_complete()
        assert app._data_transfer_progress.result.kind == "providers"
        assert (tmp_path / "target" / "config" / "providers.json").is_file()


@pytest.mark.asyncio
async def test_archive_detection_keeps_navigation_responsive_and_discards_replaced_read(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    transfer = build_local_data_transfer_service(tmp_path / "source")
    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    transfer.transfer(DataTransferRequest(action="export", kind="providers", path=first))
    transfer.transfer(DataTransferRequest(action="export", kind="mcp", path=second))
    app = OuroTuiApp(home=tmp_path / "target")
    entered, release = asyncio.Event(), asyncio.Event()
    identify = app._controller.data_archive_kind

    async def delayed(path):
        if path == first:
            entered.set()
            await release.wait()
        return await identify(path)

    monkeypatch.setattr(app._controller, "data_archive_kind", delayed)
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "import")
        form = app.screen
        try:
            await _choose_menu_row(app, pilot, "path")
            await _choose_menu_row(app, pilot, "file:one.zip")
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert any(row.loading for row in form._choices)
            assert next(row for row in form._choices if row.value == "submit").disabled
            await _choose_menu_row(app, pilot, "path")
            await _choose_menu_row(app, pilot, "file:two.zip")
            await app.workers.wait_for_complete()
            assert form._kind == "mcp"
            assert form._request().path == second
            assert not any(row.loading for row in form._choices)
        finally:
            release.set()
        await pilot.pause()
        assert form._kind == "mcp"
        assert app._data_transfer_progress.request is None


@pytest.mark.asyncio
@pytest.mark.parametrize("picker", ("selection", "path"))
async def test_selection_loading_can_be_cancelled_without_reopening_picker(
    tmp_path, monkeypatch, picker
):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    build_local_session_services(home).sessions.create(session_id="ses_one")
    app = OuroTuiApp(home=home)
    entered, release = asyncio.Event(), asyncio.Event()
    load_items = (
        app._controller.data_transfer_items
        if picker == "selection"
        else ArchivePathScreen._path_choices
    )

    async def delayed(request):
        entered.set()
        await release.wait()
        return await load_items(request)

    if picker == "selection":
        monkeypatch.setattr(app._controller, "data_transfer_items", delayed)
    else:
        monkeypatch.setattr(ArchivePathScreen, "_path_choices", delayed)
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "export", "sessions")
        form = app.screen
        await _choose_menu_row(app, pilot, picker)
        if picker == "selection":
            await _choose_menu_row(app, pilot, "selected")
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert app.screen is form
        if picker == "selection":
            assert next(row for row in form._choices if row.value == "selection").loading
        await pilot.press("escape")
        menu = app.screen
        assert menu is not form
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is menu


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_import_form_applies_existing_session_policy(tmp_path, monkeypatch, replace):
    monkeypatch.chdir(tmp_path)
    source, target = tmp_path / "source", tmp_path / "target"
    build_local_session_services(source).sessions.create(session_id="ses_one", title="Incoming")
    build_local_session_services(target).sessions.create(session_id="ses_one", title="Original")
    archive = tmp_path / "sessions.zip"
    build_local_data_transfer_service(source).transfer(
        DataTransferRequest(action="export", kind="sessions", path=archive)
    )
    app = OuroTuiApp(home=target)
    async with app.run_test(size=(100, 35)) as pilot:
        async with asyncio.timeout(10):
            while app._controller._application is None:
                await pilot.pause()
        await app.workers.wait_for_complete()
        await _open_data_menu(app, pilot, "import")
        form = app.screen
        await _choose_menu_row(app, pilot, "path")
        await _choose_menu_row(app, pilot, "file:sessions.zip")
        await app.workers.wait_for_complete()
        assert next(row for row in form._choices if row.value == "replace").trailing == "Skip"
        if replace:
            await _choose_menu_row(app, pilot, "replace")
            assert (
                next(row for row in form._choices if row.value == "replace").trailing == "Replace"
            )
        await _choose_menu_row(app, pilot, "submit")
        await app.workers.wait_for_complete()
        result = app._data_transfer_progress.result
        assert result is not None
        assert (result.count, result.skipped, result.replaced) == (
            (1, 0, 1) if replace else (0, 1, 0)
        )
        assert build_local_session_services(target)._store.load_session_record("ses_one").title == (
            "Incoming" if replace else "Original"
        )
