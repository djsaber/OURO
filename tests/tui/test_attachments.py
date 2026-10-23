from __future__ import annotations

import asyncio
import re
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from threading import Event

import pytest
from textual import events
from textual._xterm_parser import XTermParser
from textual.app import App
from textual.worker import WorkerCancelled

import ouro.tui.composer.attachment_paths as attachment_paths
from ouro.core.attachment_input import separate_attachments
from ouro.core.errors import PersistenceError, UnsafeResourcePathError
from ouro.core.input_content import InputAttachment, InputContent
from ouro.core.messages import MessageRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.attachment_paths import pasted_files
from ouro.tui.composer.attachment_picker import AttachmentPicker
from ouro.tui.composer.drafts import TuiInputDraftStore
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.composer.queue import QueuedInputPanel
from ouro.tui.composer.surface import WelcomePanel
from ouro.tui.conversation.transcript import project_transcript_entries
from ouro.tui.conversation.view import ConversationView
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.tui.helpers import command_popup_text, run_ready_app
from tests.tui.test_history_commands import application_at


def test_owned_paste_source_imports_through_an_aliased_temporary_root(tmp_path, monkeypatch):
    actual = tmp_path / "temporary"
    actual.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    @contextmanager
    def aliased_directory(**options):
        with TemporaryDirectory(dir=alias, **options) as directory:
            yield directory

    monkeypatch.setattr(attachment_paths, "TemporaryDirectory", aliased_directory)
    resources = application_at(tmp_path / "home").runtime.resources
    identity = resources.reserve()
    text = "  中文😀\r\n" * 600
    with attachment_paths.pasted_text_file(text) as source:
        imported = resources.import_prepared_resource(identity, source).resource
        assert imported.size == len(text.encode("utf-8"))
        assert source == source.resolve()
        external_alias = alias / source.relative_to(actual.resolve())
        with pytest.raises(UnsafeResourcePathError, match="symbolic links"):
            resources.import_prepared_resource(identity, external_alias)
    assert not source.exists()


@pytest.mark.parametrize("prompt", ("Explain this file", ""))
def test_attachment_input_appears_once_in_conversation(prompt):
    item = InputAttachment(resource_id="r", name="SECURITY.md")
    messages = separate_attachments(
        MessageRecord.text(session_id="s", role="user", text=prompt), (item,)
    )
    entries, _ = project_transcript_entries(
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
        await pilot.press("@", *"file")
        popup = app.query_one(CommandPopup)
        read_directory = AttachmentPicker.read_directory
        loop = asyncio.get_running_loop()
        display = app._display

        def without_spinner(value):
            return value.translate(dict.fromkeys(map(ord, app.tui_theme.spinner_frames), "›"))

        def snapshot():
            region = popup.region
            return region, tuple(
                without_spinner(strip.crop(region.x, region.right).text)
                for strip in app.screen._compositor.render_strips()[region.y : region.bottom]
            )

        frames = []

        def capture(screen, renderable):
            if screen is app.screen and renderable is not None and not app._batch_count:
                frames.append(snapshot())
            display(screen, renderable)

        monkeypatch.setattr(app, "_display", capture)
        for index, destination in enumerate((tmp_path, child, tmp_path)):
            entered, release = asyncio.Event(), Event()

            def delayed_read(directory, started=entered, finished=release):
                loop.call_soon_threadsafe(started.set)
                assert finished.wait(timeout=10)
                return read_directory(directory)

            monkeypatch.setattr(AttachmentPicker, "read_directory", staticmethod(delayed_read))
            before = snapshot()
            visible = without_spinner(command_popup_text(popup).plain)
            frames.clear()
            try:
                if index == 0:
                    await pilot.press("enter")
                else:
                    app._load_attachment_directory(destination)
                await asyncio.wait_for(entered.wait(), timeout=5)
                await pilot.pause()
                assert snapshot() == before
                assert without_spinner(command_popup_text(popup).plain) == visible
                assert any(
                    frame in command_popup_text(popup).plain
                    for frame in app.tui_theme.spinner_frames
                )
                first_frame = command_popup_text(popup).plain
                await pilot.pause(app.tui_theme.spinner_interval_seconds * 1.2)
                assert command_popup_text(popup).plain != first_frame
                if index < 2:
                    query = "file-3" if index == 0 else "nested-3"
                    composer.set_attachment_query(query)
                    await pilot.press("down", "enter", "space")
                    await pilot.pause()
                    assert composer.text == f"@file {query}"
                    assert without_spinner(command_popup_text(popup).plain) == visible
                assert all(frame == before for frame in frames)
            finally:
                release.set()
                await app.workers.wait_for_complete(
                    [w for w in app.workers if w.group == "attachment-directory"]
                )
            await pilot.pause()
            picker = app._attachment_picker
            assert picker.directory == destination and popup._heading == str(destination)
            assert composer.text == ("@file file-3", "@file nested-3", "@file ")[index]
            assert popup.region.bottom == composer.region.y
            assert popup._spinner_timer is None
            after = snapshot()
            assert frames and all(frame in (before, after) for frame in frames)


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
        visible = command_popup_text(popup)
        errors = []

        def unreadable(_directory):
            raise PermissionError("Directory access denied")

        monkeypatch.setattr(picker, "read_directory", unreadable)
        monkeypatch.setattr(app, "notify", lambda message, **_kwargs: errors.append(message))
        await app._load_attachment_directory(child).wait()
        await pilot.pause()
        assert errors == ["Directory access denied"]
        assert command_popup_text(popup) == visible and composer.text == "@file child"
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
        assert session_id is None
        assert application.runtime.store.list_sessions() == []
        if source == "picker":
            assert session_id is None
            assert composer.input_content.attachments[0].kind == "reference"
        await pilot.press("enter")
        if workers := [w for w in app.workers if w.group == "run"]:
            await asyncio.wait_for(app.workers.wait_for_complete(workers), timeout=10)
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
        rows = command_popup_text(popup).plain.splitlines()
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
        rows = command_popup_text(popup).plain.splitlines()
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


@pytest.mark.parametrize("key, mode", (("enter", "follow_up"), ("ctrl+g", "steer")))
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

        async def fail(_events, *, input_messages):
            raise ValueError("view refresh failed")

        monkeypatch.setattr(app, "_apply_runtime_event_batch", fail)
        await pilot.press("enter")
        await pilot.pause()
        if workers := [w for w in app.workers if w.group == "run"]:
            await asyncio.wait_for(app.workers.wait_for_complete(workers), timeout=10)
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
            app.on_composer_submitted(
                Composer.Submitted(content.text, "follow_up", content.attachments)
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await app._open_session(second.session_id)
            app.query_one(Composer).load_text("second draft")
        finally:
            release.set()
        await app.workers.wait_for_complete(
            [worker for worker in app.workers if worker.group == "composer-input"]
        )
        assert app.query_one(Composer).input_content == InputContent(text="second draft")
        assert app._input_drafts.content_for(first.session_id) == content
        assert not application.runtime.store.load_runtime_session(second.session_id).messages


@pytest.mark.parametrize("existing", (False, True))
@pytest.mark.parametrize("operation", ("upload", "reference"))
async def test_attachment_preparation_keeps_focus_and_blocks_edits(
    tmp_path, monkeypatch, existing, operation
):
    application = application_at(tmp_path / "home")
    origin = application.runtime.create_session().session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    path = tmp_path / "material.txt"
    path.write_text("material", encoding="utf-8")
    async with run_ready_app(app) as pilot:
        entered, release = asyncio.Event(), asyncio.Event()
        method = "import_attachment" if operation == "upload" else "reference_files"
        prepare = getattr(app._controller, method)

        async def delayed_prepare(*args):
            entered.set()
            await release.wait()
            return await prepare(*args)

        monkeypatch.setattr(app._controller, method, delayed_prepare)
        composer = app.query_one(Composer)
        await pilot.press(*"draft")
        await pilot.pause()
        border = composer.styles.border_left
        selection = composer.selection
        frames = []
        display = app._display

        def capture(screen, renderable):
            if screen is app.screen and renderable is not None and not app._batch_count:
                frames.append((composer.disabled, composer.has_focus, composer.styles.border_left))
            display(screen, renderable)

        monkeypatch.setattr(app, "_display", capture)
        pending = (
            app._attach_files((path,)) if operation == "upload" else app._reference_files((path,))
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            app._sync_input_state(None)
            await pilot.pause()
            assert composer.import_pending
            assert not composer.disabled and composer.has_focus
            assert composer.styles.border_left == border
            await pilot.click(composer, offset=(1, 0))
            assert composer.selection == selection
            for key in ("x", "backspace", "ctrl+z", "ctrl+enter", "up", "enter"):
                await pilot.press(key)
                assert composer.input_content == InputContent(text="draft")
            composer.post_message(events.Paste("another paste"))
            await pilot.pause()
            assert composer.input_content == InputContent(text="draft")
            assert app._controller.session_id == origin
        finally:
            release.set()
            await pending.wait()
        await pilot.pause()
        assert composer.has_focus and not composer.disabled and not composer.import_pending
        assert composer.styles.border_left == border
        assert composer.input_content.text == "draft"
        assert composer.input_content.attachments[0].name == path.name
        assert frames and all(frame == (False, True, border) for frame in frames)


@pytest.mark.parametrize("operation", ("upload", "reference"))
async def test_preparation_completion_preserves_focus_moved_to_conversation(
    tmp_path, monkeypatch, operation
):
    application = application_at(tmp_path / "home")
    session = application.runtime.create_session()
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    path = tmp_path / "material.txt"
    path.write_text("material", encoding="utf-8")
    async with run_ready_app(app) as pilot:
        entered, release = asyncio.Event(), asyncio.Event()
        method = "import_attachment" if operation == "upload" else "reference_files"
        prepare = getattr(app._controller, method)

        async def delayed_prepare(*args):
            entered.set()
            await release.wait()
            return await prepare(*args)

        monkeypatch.setattr(app._controller, method, delayed_prepare)
        composer = app.query_one(Composer)
        conversation = app.query_one(ConversationView)
        await conversation.add_user("Read while preparing")
        await pilot.pause()
        worker = (
            app._attach_files((path,)) if operation == "upload" else app._reference_files((path,))
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            await pilot.click(conversation, offset=(2, conversation.size.height - 1))
            assert app.focused is conversation
        finally:
            release.set()
            await worker.wait()
        await pilot.pause()
        assert app.focused is conversation
        assert not composer.has_focus and not composer.import_pending
        assert composer.input_content.attachments[0].name == path.name


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


@pytest.mark.parametrize("existing", (False, True))
async def test_terminal_file_paste_uploads_and_submits_on_both_pages(tmp_path, existing):
    root = tmp_path / "home"
    application = application_at(root)
    runtime = application.runtime
    origin = runtime.create_session().session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    path = tmp_path / "测试 文件.txt"
    path.write_text("file material", encoding="utf-8")
    parser = XTermParser()
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("before after")
        composer.move_cursor((0, 7))
        for event in parser.feed("\x1b[O"):
            app.post_message(event)
        await pilot.pause()
        assert not app.app_focus and app.focused is None
        # Windows Terminal sends the copied file's unquoted path as a paste.
        for event in parser.feed(f"\x1b[200~{path}\x1b[201~"):
            app.post_message(event)
        await pilot.pause()
        if workers := [w for w in app.workers if w.group == "attachment-import"]:
            await app.workers.wait_for_complete(workers)
        content = composer.input_content
        assert content.text == "before after"
        assert len(content.attachments) == 1
        attachment = content.attachments[0]
        assert attachment.kind == "upload" and attachment.name == path.name
        assert attachment.text_offset == 7
        assert app.app_focus and app.focused is composer
        assert app._controller.session_id == origin
        assert len(runtime.store.list_sessions()) == int(existing)
        assert TuiInputDraftStore(root).load().content_for(origin) == content
        identity = origin or app._input_drafts.welcome_resource_id
        uploaded = runtime.resources.directory_for(identity) / attachment.name
        path.unlink()
        await pilot.press("enter")
        if workers := [w for w in app.workers if w.group == "run"]:
            await asyncio.wait_for(app.workers.wait_for_complete(workers), timeout=10)
        await pilot.pause()
        assert app._controller.session_id == identity
        snapshot = runtime.store.load_runtime_session(identity)
        body = next(message for message in snapshot.messages if not message.scaffold)
        assert body.text_content == content.text
        assert body.input_attachments == content.attachments
        assert uploaded.read_text(encoding="utf-8") == "file material"
        assert composer.input_content.is_empty


@pytest.mark.parametrize("existing", (False, True))
@pytest.mark.parametrize("source", ("terminal", "terminal-after-blur", "clipboard"))
async def test_long_text_paste_uploads_and_restores_on_both_pages(tmp_path, existing, source):
    root = tmp_path / "home"
    application = application_at(root)
    origin = application.runtime.create_session().session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    text = "  中文😀\r\n\t" * 600 + " trailing \n"
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("before after")
        composer.move_cursor((0, 7))
        if source == "terminal-after-blur":
            app.post_message(events.AppBlur())
            await pilot.pause()
            assert not app.app_focus and app.focused is None
        if source.startswith("terminal"):
            app.post_message(events.Paste(text))
        else:
            composer._clipboard_reader = lambda: text
            await composer._paste_system_clipboard().wait()
        await pilot.pause()
        if workers := [w for w in app.workers if w.group == "attachment-import"]:
            await app.workers.wait_for_complete(workers)
        content = composer.input_content
        assert content.text == "before after"
        assert len(content.attachments) == 1
        assert app.app_focus and app.focused is composer
        attachment = content.attachments[0]
        assert attachment.kind == "upload" and attachment.text_offset == 7
        assert re.fullmatch(r"pasted-text_[0-9a-f]{32}\.txt", attachment.name)
        identity = origin or app._input_drafts.welcome_resource_id
        file = application.runtime.resources.directory_for(identity) / attachment.name
        assert file.read_bytes() == text.encode("utf-8")
        assert app._controller.session_id == origin
        assert len(application.runtime.store.list_sessions()) == int(existing)
        assert TuiInputDraftStore(root).load().content_for(origin) == content
        composer.undo()
        assert composer.input_content == InputContent(text="before after")
        composer.redo()
        assert composer.input_content == content
        await pilot.pause()
    restarted = OuroTuiApp(application=application_at(root), resume_session_id=origin)
    async with run_ready_app(restarted):
        assert restarted.query_one(Composer).input_content == content
        assert file.read_bytes() == text.encode("utf-8")


async def test_only_the_current_paste_length_triggers_text_attachment(tmp_path):
    app = OuroTuiApp(application=application_at(tmp_path))
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("x" * 3999)
        composer.move_cursor(composer.document.end)
        await pilot.press("y", "z")
        assert composer.input_content == InputContent(text="x" * 3999 + "yz")
        text = "文" * 4000
        for _ in range(2):
            app.post_message(events.Paste(text))
            await pilot.pause()
        assert composer.input_content == InputContent(text="x" * 3999 + "yz" + text * 2)
        assert app._input_drafts.welcome_resource_id is None


async def test_each_long_paste_uses_an_independent_named_attachment(tmp_path):
    app = OuroTuiApp(application=application_at(tmp_path))
    async with run_ready_app(app) as pilot:
        text = "x" * 4001
        for _ in range(2):
            app.post_message(events.Paste(text))
            await pilot.pause()
            if workers := [w for w in app.workers if w.group == "attachment-import"]:
                await app.workers.wait_for_complete(workers)
        items = app.query_one(Composer).input_content.attachments
        assert len(items) == 2
        assert items[0].name != items[1].name
        assert items[0].resource_id != items[1].resource_id


async def test_long_paste_import_failure_preserves_draft_and_releases_source(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    app = OuroTuiApp(application=application)
    sources = []

    def fail(_identity, path):
        sources.append(path)
        assert path.read_bytes() == b"x" * 4001
        raise PersistenceError("upload failed")

    monkeypatch.setattr(application.runtime.resources, "import_prepared_resource", fail)
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("keep this")
        app.post_message(events.Paste("x" * 4001))
        await pilot.pause()
        if workers := [w for w in app.workers if w.group == "attachment-import"]:
            await app.workers.wait_for_complete(workers)
        assert composer.input_content == InputContent(text="keep this")
        assert not composer.import_pending
        assert application.runtime.store.list_sessions() == []
        assert len(sources) == 1 and not sources[0].exists()


@pytest.mark.parametrize("existing", (False, True))
async def test_cancelled_long_paste_keeps_source_until_import_finishes(
    tmp_path, monkeypatch, existing
):
    application = application_at(tmp_path)
    origin = application.runtime.create_session().session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    resources = application.runtime.resources
    operation = "import_resource" if existing else "import_prepared_resource"
    import_file = getattr(resources, operation)
    sources, imported = [], []
    reached, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()

    def delayed(identity, path, **kwargs):
        sources.append(path)
        loop.call_soon_threadsafe(reached.set)
        assert release.wait(10)
        result = import_file(identity, path, **kwargs)
        imported.append(result)
        return result

    monkeypatch.setattr(resources, operation, delayed)
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("keep this")
        worker = app._attach_files("x" * 4001)
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            worker.cancel()
            await pilot.pause()
            assert composer.import_pending
            assert sources[0].read_bytes() == b"x" * 4001
        finally:
            release.set()
        with pytest.raises(WorkerCancelled):
            await worker.wait()
        assert len(imported) == 1
        assert not sources[0].exists()
        assert not composer.import_pending
        assert composer.input_content == InputContent(text="keep this")


@pytest.mark.parametrize("outcome", ("cancel", "failure"))
async def test_long_paste_cleanup_always_releases_input(tmp_path, monkeypatch, outcome):
    app = OuroTuiApp(application=application_at(tmp_path))
    reached, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    source_file = attachment_paths.pasted_text_file
    sources, errors = [], []

    @contextmanager
    def delayed_cleanup(text):
        with source_file(text) as path:
            sources.append(path)
            yield path
            loop.call_soon_threadsafe(reached.set)
            assert release.wait(10)
            if outcome == "failure":
                raise OSError("Temporary source cleanup failed")

    monkeypatch.setattr("ouro.tui.app.pasted_text_file", delayed_cleanup)
    async with run_ready_app(app) as pilot:
        monkeypatch.setattr(app, "notify", lambda message, **_kwargs: errors.append(message))
        composer = app.query_one(Composer)
        worker = app._attach_files("x" * 4001)
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            if outcome == "cancel":
                worker.cancel()
            await pilot.pause()
            assert composer.import_pending
        finally:
            release.set()
        if outcome == "cancel":
            with pytest.raises(WorkerCancelled):
                await worker.wait()
        else:
            await worker.wait()
            assert errors == ["Temporary source cleanup failed"]
        assert not sources[0].exists()
        assert not composer.import_pending and composer.has_focus
        assert len(composer.input_content.attachments) == 1
        await pilot.press("x")
        assert composer.input_content.text == "x"


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
