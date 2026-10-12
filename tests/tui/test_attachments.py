from __future__ import annotations

import asyncio
from threading import Event

import pytest
from textual.app import App

from ouro.core.attachment_input import separate_attachments
from ouro.core.input_content import InputAttachment, InputContent
from ouro.core.messages import MessageRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.attachment_paths import pasted_files
from ouro.tui.attachment_picker import AttachmentPicker
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.widgets import (
    CommandPopup,
    Composer,
    ConversationView,
    QueuedInputPanel,
    WelcomePanel,
    _project_transcript_entries,
)
from tests.tui.helpers import run_ready_app
from tests.tui.test_history_commands import application_at


@pytest.mark.parametrize("prompt", ("Explain this file", ""))
def test_attachment_input_appears_once_in_conversation(prompt):
    item = InputAttachment(resource_id="r", name="SECURITY.md")
    messages = separate_attachments(
        MessageRecord.text(session_id="s", role="user", text=prompt), (item,)
    )
    entries, _ = _project_transcript_entries(
        tuple(messages),
        (),
        (),
        mutable_tool_call_keys=frozenset(),
        visible_scaffold_delegation_id=None,
    )
    assert [entry.text for entry in entries if entry.kind == "user"] == [
        f"{prompt}\nSECURITY.md".lstrip("\n")
    ]


def test_batch_path_paste_is_conservative(tmp_path):
    one, two = tmp_path / "测试 文件.txt", tmp_path / "two.txt"
    one.write_text("one", encoding="utf-8")
    two.write_text("two", encoding="utf-8")
    assert pasted_files(f'"{one}" "{two}"') == (one, two)
    assert pasted_files(f"'{one}'\n'{two}'") == (one, two)
    assert pasted_files(one.as_uri()) == (one,)
    assert pasted_files(str(one)) == (one,)
    for value in (
        f"look at {one}",
        f'"{one}" --flag',
        f'"{one}',
        str(tmp_path),
        "a@b.com",
        "$(command)",
    ):
        assert pasted_files(value) == ()


def test_picker_names_share_left_edge_and_only_files_have_selection_marks(tmp_path):
    (tmp_path / "child").mkdir()
    path = tmp_path / "notes.txt"
    path.write_text("notes")
    picker = AttachmentPicker(tmp_path)
    picker.entries = picker.read_directory(tmp_path)
    picker.toggle(str(path))
    arguments = picker.arguments("", DEFAULT_TUI_THEME)
    assert [item.display_label for item in arguments] == [
        "..",
        "child",
        "notes.txt",
    ]
    assert [item.checked for item in arguments] == [None, None, True]
    assert all(not item.description for item in arguments)
    assert picker.arguments("missing", DEFAULT_TUI_THEME) == ()
    picker.toggle(str(path))
    assert picker.arguments("notes", DEFAULT_TUI_THEME)[0].checked is False


async def test_directory_navigation_keeps_visible_listing_until_read_completes(
    tmp_path, monkeypatch
):
    child = tmp_path / "child"
    child.mkdir()
    for index in range(6):
        (tmp_path / f"file-{index}.txt").write_text("parent")
        (child / f"nested-{index}.txt").write_text("child")
    app = OuroTuiApp(application=application_at(tmp_path / "home"))
    async with run_ready_app(app) as pilot:
        app._active_workspace = str(tmp_path)
        composer = app.query_one(Composer)
        await pilot.press("@", *"file", "enter")
        await app.workers.wait_for_complete(
            [w for w in app.workers if w.group == "attachment-directory"]
        )
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        picker = app._attachment_picker
        read_directory = picker.read_directory
        loop = asyncio.get_running_loop()
        for destination in (child, tmp_path):
            entered, release = asyncio.Event(), Event()

            def delayed_read(directory, started=entered, finished=release):
                loop.call_soon_threadsafe(started.set)
                assert finished.wait(timeout=10)
                return read_directory(directory)

            monkeypatch.setattr(picker, "read_directory", delayed_read)
            before = (popup._arguments, popup._heading, popup.region, composer.text)
            visible = popup._content.content
            worker = app._load_attachment_directory(destination)
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                await pilot.pause()
                assert (popup._arguments, popup._heading, popup.region, composer.text) == before
                assert popup._content.content == visible
                if destination == child:
                    composer.set_attachment_query("nested-3")
                    await pilot.pause()
                    assert popup._content.content == visible
            finally:
                release.set()
                await worker.wait()
            await pilot.pause()
            assert picker.directory == destination and popup._heading == str(destination)
            assert composer.text == ("@file nested-3" if destination == child else "@file ")
            assert popup.region.bottom == composer.region.y


async def test_unreadable_directory_retains_listing_filter_and_selection(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    path = tmp_path / "notes.txt"
    path.write_text("notes")
    app = OuroTuiApp(application=application_at(tmp_path / "home"))
    async with run_ready_app(app) as pilot:
        app._active_workspace = str(tmp_path)
        await pilot.press("@", *"file", "enter")
        await app.workers.wait_for_complete(
            [w for w in app.workers if w.group == "attachment-directory"]
        )
        picker = app._attachment_picker
        picker.toggle(str(path))
        composer = app.query_one(Composer)
        composer.set_attachment_query("child")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        visible = popup._content.content
        errors = []

        def unreadable(_directory):
            raise PermissionError("Directory access denied")

        monkeypatch.setattr(picker, "read_directory", unreadable)
        monkeypatch.setattr(app, "notify", lambda message, **_kwargs: errors.append(message))
        await app._load_attachment_directory(child).wait()
        await pilot.pause()
        assert errors == ["Directory access denied"]
        assert popup._content.content == visible and composer.text == "@file child"
        assert picker.directory == tmp_path and picker.selected == [path]
        assert not picker.loading


@pytest.mark.parametrize("source", ("picker", "paste"))
async def test_welcome_upload_stays_in_draft_until_message_is_sent(tmp_path, source):
    path = tmp_path / "notes.txt"
    path.write_text("notes")
    application = application_at(tmp_path / "home")
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        app._active_workspace = str(tmp_path)
        composer = app.query_one(Composer)
        if source == "picker":
            await pilot.press("@", *"file", "enter")
            await app.workers.wait_for_complete(
                [w for w in app.workers if w.group == "attachment-directory"]
            )
            composer.set_attachment_query(path.name)
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete(
                [w for w in app.workers if w.group in {"attachment-import", "attachment-reference"}]
            )
        else:
            await app._attach_files(str(path)).wait()
        await pilot.pause()
        assert app.query_one(WelcomePanel).has_class("welcome-page")
        assert app.query_one(ConversationView).showing_welcome
        assert app._projection is None
        assert composer.input_content.attachments[0].name == path.name
        session_id = app._controller.session_id
        if source == "picker":
            assert session_id is None
            assert composer.input_content.attachments[0].kind == "reference"
        else:
            snapshot = application.runtime.store.load_runtime_session(session_id)
            assert not snapshot.runs and not snapshot.messages
        await pilot.press("enter")
        await app.workers.wait_for_complete([w for w in app.workers if w.group == "run"])
        await pilot.pause()
        assert not app.query_one(WelcomePanel).has_class("welcome-page")
        assert not app.query_one(ConversationView).showing_welcome
        session_id = app._controller.session_id
        snapshot = application.runtime.store.load_runtime_session(session_id)
        assert snapshot.runs and snapshot.messages[0].input_attachments
        assert app._active_title == snapshot.session.title == "New conversation"
        assert snapshot.session.title_source == "placeholder"
        assert composer.input_content.is_empty


async def test_picker_retains_multiselection_across_directories_and_confirms_directly(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    one, two = tmp_path / "one.txt", child / "two.txt"
    one.write_text("one")
    two.write_text("two")
    app = OuroTuiApp(application=application_at(tmp_path / "home"))
    async with run_ready_app(app) as pilot:
        app._active_workspace = str(tmp_path)
        screen = app.screen
        composer = app.query_one(Composer)
        await pilot.press("@", *"file", "enter")
        await app.workers.wait_for_complete(
            [w for w in app.workers if w.group == "attachment-directory"]
        )
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert app.screen is screen and app.focused is composer
        assert popup.display and popup.region.bottom == composer.region.y
        folder = next(item for item in popup._arguments if item.value == str(child))
        assert folder.display_label == "child"
        rows = popup._content.content.plain.splitlines()
        for name in ("..", "child", "one.txt"):
            assert next(row for row in rows if name in row).index(name) == 4
        assert next(row for row in rows if "one.txt" in row).strip() == (
            f"{app.tui_theme.unselected_choice_marker} one.txt"
        )
        for marker in (
            app.tui_theme.selected_choice_marker,
            app.tui_theme.unselected_choice_marker,
        ):
            for name in ("..", "child"):
                assert marker not in next(row for row in rows if name in row)
        composer.set_attachment_query("one.txt")
        await pilot.pause()
        await pilot.press("space")
        picker = app._attachment_picker
        assert picker.selected == [one]
        rows = popup._content.content.plain.splitlines()
        assert next(row for row in rows if "one.txt" in row).strip() == (
            f"› {app.tui_theme.selected_choice_marker} one.txt"
        )
        composer.set_attachment_query("child")
        await pilot.pause()
        await pilot.press("space")
        assert picker.selected == [one]
        await pilot.press("enter")
        await app.workers.wait_for_complete(
            [w for w in app.workers if w.group == "attachment-directory"]
        )
        assert picker.directory == child
        composer.set_attachment_query("two.txt")
        await pilot.pause()
        await pilot.press("space")
        assert picker.selected == [one, two]
        composer.set_attachment_query("")
        await pilot.pause()
        await pilot.press("up", "enter")
        await app.workers.wait_for_complete(
            [w for w in app.workers if w.group == "attachment-directory"]
        )
        assert picker.directory == tmp_path and picker.selected == [one, two]
        composer.set_attachment_query("one.txt")
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete(
            [w for w in app.workers if w.group in {"attachment-import", "attachment-reference"}]
        )
        await pilot.pause()
        assert [item.name for item in composer.input_content.attachments] == [one.name, two.name]
        assert not composer.attachment_menu_open and not popup.display
        assert app.screen is screen


async def test_import_queue_cancel_and_draft_restart_restore_exact_input(tmp_path):
    application = application_at(tmp_path)
    runtime = application.runtime
    session = runtime.create_session(title="queued attachments")
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    one, two = tmp_path / "测试 文件.txt", tmp_path / "two.bin"
    one.write_text("data", encoding="utf-8")
    two.write_bytes(b"\x00\xff")
    async with run_ready_app(app, size=(100, 32)) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("before after")
        composer.move_cursor((0, 7))
        await app._attach_files(f'"{one}" "{two}"').wait()
        await pilot.pause()
        content = composer.input_content
        assert content.text == "before after"
        assert [item.name for item in content.attachments] == [one.name, two.name]
        assert [item.text_offset for item in content.attachments] == [7, 7]
        session_id = app._controller.session_id
        snapshot = runtime.store.load_runtime_session(session_id)
        assert not snapshot.runs and not snapshot.messages
        _, queued, _ = runtime.queued_inputs.enqueue(
            snapshot=snapshot,
            prompt=content.text,
            attachments=content.attachments,
            resource_refs=content.resource_refs,
            mode="follow_up",
            title=None,
            role="user",
            scaffold=False,
        )
        composer.load_text("new draft")
        await app._sync_queued_inputs()
        panel = app.query_one(QueuedInputPanel)
        assert panel.inputs[0].attachments == content.attachments
        assert app._queued_input_arguments()[0].trailing_badge == "2 attachments"
        await app._unqueue_message(queued.queue_id)
        restored = composer.input_content
        assert restored.text == "new draft\nbefore after"
        assert [item.text_offset for item in restored.attachments] == [17, 17]
        assert not runtime.pending_user_inputs(session_id)
        await pilot.pause()
    restarted = OuroTuiApp(application=application_at(tmp_path), resume_session_id=session_id)
    async with run_ready_app(restarted):
        assert restarted.query_one(Composer).input_content == restored


@pytest.mark.parametrize("key, mode", (("enter", "follow_up"), ("ctrl+j", "steer")))
async def test_composer_at_and_plain_paste_do_not_confuse_attachment_identity(
    tmp_path, monkeypatch, key, mode
):
    app = OuroTuiApp(application=application_at(tmp_path))
    submitted = []
    monkeypatch.setattr(
        app,
        "_submit_run",
        lambda text, mode, *, attachments, skills: submitted.append((text, mode, attachments)),
    )
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("name")
        composer.move_cursor((0, 4))
        await pilot.press("@")
        assert composer.input_content == InputContent(text="name@")
        composer.load_text("check ")
        composer.move_cursor((0, 0))
        await pilot.press("@", *"file", "enter")
        await pilot.pause()
        assert composer.attachment_menu_open and app.query_one(CommandPopup).display
        await pilot.press("escape")
        await pilot.pause()
        assert composer.input_content == InputContent(text="check ")
        composer.move_cursor((0, 6))
        await app._attach_files("/ordinary text to paste").wait()
        assert composer.input_content.text == "check /ordinary text to paste"

        attachment = InputAttachment(resource_id="r", name="file.txt", text_offset=7)
        for content, position, expected in (
            (InputContent(), 0, "@file missing text"),
            (
                InputContent(text="before after", attachments=(attachment,)),
                0,
                "@file missing textbefore after",
            ),
        ):
            composer.load_input(content)
            composer.move_cursor((0, position))
            await pilot.press("@", *"file", "enter")
            if workers := [w for w in app.workers if w.group == "attachment-directory"]:
                await app.workers.wait_for_complete(workers)
            await pilot.press(*"missing text")
            await pilot.pause()
            assert app.query_one(CommandPopup).display
            assert app.query_one(CommandPopup).selected_argument is None
            await pilot.press(key)
            await pilot.pause()
            text, submitted_mode, attachments = submitted[-1]
            assert (text, submitted_mode) == (expected, mode)
            assert attachments == tuple(
                item.model_copy(
                    update={"text_offset": item.text_offset + len("@file missing text")}
                )
                for item in content.attachments
            )
            assert composer.input_content.is_empty
            assert not composer.attachment_menu_open and app._attachment_picker is None


async def test_transient_commands_preserve_attachment_spans():
    app = App()
    composer = Composer()
    async with app.run_test():
        await app.mount(composer)
        content = InputContent(
            text="draft",
            attachments=(InputAttachment(resource_id="r", name="file.txt", text_offset=5),),
        )
        composer.load_input(content)
        composer.load_command("/model ", draft=composer.text)
        assert composer.draft_content == content
        composer.restore_command_draft(composer.command_context()[1])
        assert composer.input_content == content


async def test_accepted_input_is_not_restored_when_rendering_fails(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    session = application.runtime.create_session(title="accepted")
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        path = tmp_path / "file.txt"
        path.write_text("data", encoding="utf-8")
        await app._attach_files(str(path)).wait()

        async def fail(_result):
            raise ValueError("view refresh failed")

        monkeypatch.setattr(app, "_apply_run_result", fail)
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete([w for w in app.workers if w.group == "run"])
        assert app.query_one(Composer).input_content.is_empty
        assert (
            application.runtime.store.load_runtime_session(session.session_id)
            .messages[0]
            .input_attachments
        )


async def test_submit_preflight_restores_original_session_after_navigation(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    first = application.runtime.create_session(title="first")
    second = application.runtime.create_session(title="second")
    app = OuroTuiApp(application=application, resume_session_id=first.session_id)
    async with run_ready_app(app):
        entered, release = asyncio.Event(), asyncio.Event()
        models = app._controller.models

        async def delayed_models():
            entered.set()
            await release.wait()
            return await models()

        content = InputContent(
            text="original",
            attachments=(InputAttachment(resource_id="r", name="a.txt", text_offset=3),),
        )
        with monkeypatch.context() as patch:
            patch.setattr(app._controller, "models", delayed_models)
            pending = asyncio.create_task(
                app.on_composer_submitted(
                    Composer.Submitted(content.text, "follow_up", content.attachments)
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await app._open_session(second.session_id)
            app.query_one(Composer).load_text("second draft")
        finally:
            release.set()
        await pending
        assert app.query_one(Composer).input_content == InputContent(text="second draft")
        assert app._input_drafts.content_for(first.session_id) == content
        assert not application.runtime.store.load_runtime_session(second.session_id).messages


async def test_late_import_preserves_original_position_and_new_session_is_editable(
    tmp_path, monkeypatch
):
    application = application_at(tmp_path)
    first = application.runtime.create_session(title="first")
    second = application.runtime.create_session(title="second")
    app = OuroTuiApp(application=application, resume_session_id=first.session_id)
    path = tmp_path / "late.txt"
    path.write_text("material", encoding="utf-8")
    async with run_ready_app(app):
        entered, release = asyncio.Event(), asyncio.Event()
        import_attachment = app._controller.import_attachment

        async def delayed_import(session, path):
            entered.set()
            await release.wait()
            return await import_attachment(session, path)

        monkeypatch.setattr(app._controller, "import_attachment", delayed_import)
        composer = app.query_one(Composer)
        composer.load_text("before after")
        composer.move_cursor((0, 7))
        pending = app._attach_files(str(path))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await app._open_session(second.session_id)
        finally:
            release.set()
        await pending.wait()
        assert composer.input_content.is_empty
        assert not composer.disabled and not composer.import_pending
        restored = app._input_drafts.content_for(first.session_id)
        assert restored.text == "before after"
        assert restored.attachments[0].name == path.name
        assert restored.attachments[0].text_offset == 7
        await app._open_session(first.session_id)
        assert composer.input_content == restored


async def test_unsupported_draft_file_is_not_overwritten_by_new_input(tmp_path):
    path = tmp_path / "config" / "tui-input-drafts.json"
    path.parent.mkdir(parents=True)
    original = b'{"schema_version":2,"welcome":"old draft","sessions":[]}'
    path.write_bytes(original)
    app = OuroTuiApp(application=application_at(tmp_path))
    async with run_ready_app(app) as pilot:
        assert app._input_drafts_error is not None
        app.query_one(Composer).load_text("new draft")
        await pilot.pause()
    assert path.read_bytes() == original


async def test_import_keeps_original_in_ouro_data_without_writing_workspace(tmp_path):
    data = tmp_path / "ouro-data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source.txt"
    source.write_bytes(b"immutable upload")
    application = application_at(data)
    session = application.runtime.create_session(title="storage", workspace=workspace)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app):
        await app._attach_files(str(source)).wait()
        attachment = app.query_one(Composer).input_content.attachments[0]
        source.unlink()
        assert not list(workspace.iterdir())
        saved = application.runtime.resources.resource_path(
            session.session_id, attachment.resource_id
        )
        assert saved == data / "sessions" / session.session_id / "resources" / "files" / source.name
        assert saved.read_bytes() == b"immutable upload"


async def test_inline_picker_cancel_preserves_draft_cursor_and_narrow_layout(tmp_path):
    application = application_at(tmp_path)
    session = application.runtime.create_session(title="cancel menu")
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app, size=(44, 20)) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("before after")
        composer.move_cursor((0, 0))
        original, selection = composer.input_content, composer.selection
        await pilot.press("@", *"file", "enter")
        if workers := [w for w in app.workers if w.group == "attachment-directory"]:
            await app.workers.wait_for_complete(workers)
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert app.focused is composer and popup.display
        assert popup.region.bottom == composer.region.y
        assert popup.region.right <= 44
        await pilot.press(*"missing")
        await pilot.pause()
        assert popup.display and popup.selected_argument is None
        assert app._input_drafts.content_for(session.session_id) == original
        composer.set_attachment_query("")
        await pilot.pause()
        assert popup.display
        await pilot.press("escape")
        assert composer.input_content == original and composer.selection == selection
        assert not popup.display
        await pilot.press("@", "backspace")
        await pilot.pause()
        assert composer.input_content == original and not popup.display
