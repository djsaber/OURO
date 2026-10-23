from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.core.errors import ConfigError, PersistenceError
from ouro.core.input_content import InputContent
from ouro.tui.app import OuroTuiApp
from ouro.tui.draft_recovery import recover_input_drafts
from ouro.tui.input_drafts import TuiInputDrafts, TuiInputDraftStore, TuiPendingInput
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.widgets import Composer
from tests.tui.helpers import release_on_exit, run_ready_app, wait_for_worker_group, wait_until
from tests.tui.test_history_commands import application_at


@pytest.mark.parametrize("backend", ("files", "sqlite"))
@pytest.mark.parametrize("existing", (False, True))
async def test_both_pages_restore_upload_and_reference_then_submit(
    tmp_path, backend, existing, monkeypatch
):
    root = tmp_path / "home"
    source = tmp_path / "upload.txt"
    reference = tmp_path / "reference.txt"
    source.write_text("uploaded snapshot")
    reference.write_text("original reference")
    application = application_at(root, backend=backend)
    origin = application.runtime.create_session(workspace=tmp_path).session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    async with run_ready_app(app) as pilot:
        if not existing:
            await app._controller.set_workspace(str(tmp_path))
        await app._attach_files((source,)).wait()
        composer = app.query_one(Composer)
        composer.insert_attachments(await app._controller.reference_files((reference,)))
        await pilot.pause()
        await app._flush_input_drafts()
        original = composer.input_content
        saved = TuiInputDraftStore(root).load()
        assert saved.content_for(origin) == original
        identity = origin or saved.welcome_resource_id
        assert identity is not None
        assert len(application.runtime.store.list_sessions()) == int(existing)

    source.unlink()
    reference.write_text("updated reference")
    restarted_application = application_at(root, backend=backend)
    restarted = OuroTuiApp(application=restarted_application, resume_session_id=origin)

    def no_import(*_args, **_kwargs):
        pytest.fail("Restored upload must use its original resource")

    monkeypatch.setattr(
        restarted_application.runtime.resources, "import_prepared_resource", no_import
    )
    monkeypatch.setattr(restarted_application.runtime.resources, "import_resource", no_import)
    async with run_ready_app(restarted) as pilot:
        assert restarted.query_one(Composer).input_content == original
        assert restarted._controller.session_id == origin
        if not existing:
            assert restarted._controller.draft_workspace == str(tmp_path)
        await pilot.press("enter")
        workers = [w for w in restarted.workers if w.group == "run"]
        if workers:
            await asyncio.wait_for(restarted.workers.wait_for_complete(workers), 10)
        assert restarted._controller.session_id == identity
        snapshot = restarted_application.runtime.store.load_runtime_session(identity)
        assert snapshot.runs[-1].status == "completed"
        assert any("uploaded snapshot" in m.text_content for m in snapshot.messages)
        assert any("updated reference" in m.text_content for m in snapshot.messages)
        assert restarted.query_one(Composer).input_content.is_empty
        assert restarted._input_drafts.pending == ()


async def test_restored_upload_delete_undo_and_discard_do_not_create_session(tmp_path):
    source = tmp_path / "upload.txt"
    source.write_text("keep source")
    root = tmp_path / "home"
    app = OuroTuiApp(application=application_at(root))
    async with run_ready_app(app):
        await app._attach_files((source,)).wait()
        identity = app._input_drafts.welcome_resource_id
    application = application_at(root)
    restarted = OuroTuiApp(application=application)
    async with run_ready_app(restarted) as pilot:
        composer = restarted.query_one(Composer)
        original = composer.input_content
        composer.move_cursor((0, 0))
        await pilot.press("delete")
        assert composer.input_content == InputContent()
        composer.undo()
        assert composer.input_content == original
        composer.redo()
        await pilot.pause()
        assert application.runtime.store.list_sessions() == []
    assert not application.runtime.resources.directory_for(identity).exists()
    assert TuiInputDraftStore(root).load().welcome.is_empty
    assert source.read_text() == "keep source"


@pytest.mark.parametrize("accepted", (False, True))
async def test_restart_resolves_interrupted_submission_from_durable_acceptance(tmp_path, accepted):
    application = application_at(tmp_path)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app):
        identity = application.runtime.resources.reserve()
        pending = TuiPendingInput(input_id="interrupted", session_id=identity, text="original")
        drafts = TuiInputDrafts(welcome_resource_id=identity, pending=(pending,))
        if accepted:
            await application.runtime.run(
                pending.text, new_session_id=identity, input_id=pending.input_id
            )
        recovered, resume = await recover_input_drafts(app._controller, drafts)
        assert recovered.pending == ()
        if accepted:
            assert resume == identity and recovered.welcome.is_empty
        else:
            assert resume is None and recovered.welcome.text == "original"
            assert application.runtime.store.list_sessions() == []


async def test_disabled_draft_memory_releases_upload_without_removing_source(tmp_path):
    root = tmp_path / "home"
    TuiPreferencesStore(root).save(TuiPreferences(input_draft_memory_enabled=False))
    source = tmp_path / "notes.txt"
    source.write_text("source")
    application = application_at(root)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app):
        await app._attach_files((source,)).wait()
        identity = app._input_drafts.welcome_resource_id
        assert app.query_one(Composer).input_content.attachments
    assert not application.runtime.resources.directory_for(identity).exists()
    assert TuiInputDraftStore(root).load().welcome.is_empty
    assert source.read_text() == "source"
    assert application.runtime.store.list_sessions() == []


async def test_disabled_memory_releases_interrupted_unaccepted_upload(tmp_path, monkeypatch):
    root = tmp_path / "home"
    TuiPreferencesStore(root).save(TuiPreferences(input_draft_memory_enabled=False))
    source = tmp_path / "notes.txt"
    source.write_text("source")
    application = application_at(root)
    app = OuroTuiApp(application=application)
    reached = asyncio.Event()

    async def wait_before_acceptance(*_args, **_kwargs):
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app._controller, "submit", wait_before_acceptance)
    async with run_ready_app(app) as pilot:
        await app._attach_files((source,)).wait()
        identity = app._input_drafts.welcome_resource_id
        await pilot.press("enter")
        await asyncio.wait_for(reached.wait(), 5)
    assert not application.runtime.resources.directory_for(identity).exists()
    assert application.runtime.store.list_sessions() == []
    assert source.read_text() == "source"


@pytest.mark.parametrize("existing", (False, True))
async def test_both_pages_keep_input_when_draft_save_fails(tmp_path, existing, monkeypatch):
    application = application_at(tmp_path)
    origin = application.runtime.create_session().session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    recovery_started = threading.Event()
    release_recovery = threading.Event()
    recovering = False
    restore = app._restore_unsent_input

    def restore_input(*args):
        nonlocal recovering
        restore(*args)
        recovering = True

    monkeypatch.setattr(app, "_restore_unsent_input", restore_input)
    async with run_ready_app(app) as pilot, release_on_exit(release_recovery):

        def fail(_drafts):
            if recovering:
                recovery_started.set()
                release_recovery.wait()
            raise PersistenceError("draft disk unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(app._input_draft_store, "save", fail)
            composer = app.query_one(Composer)
            composer.load_text("keep my input")
            await pilot.press("enter")
            await wait_for_worker_group(app, pilot, group="run")
            await wait_until(
                recovery_started.is_set, pilot, description="restored draft save in flight"
            )
            # Restoring rejected input schedules another debounced save. Keep
            # the injected disk fault in force until that owned write settles;
            # restoring the method cannot replace a callable already in flight.
            save_task = app._input_draft_save_task
            assert save_task is not None and not save_task.done()
            release_recovery.set()
            await asyncio.shield(save_task)
            assert composer.input_content.text == "keep my input"
            assert len(application.runtime.store.list_sessions()) == int(existing)
            if existing:
                assert application.runtime.store.load_runtime_session(origin).messages == []
        await app._flush_input_drafts()
        assert TuiInputDraftStore(tmp_path).load().content_for(origin).text == "keep my input"


async def test_navigation_during_submission_save_retains_original_target(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    app = OuroTuiApp(application=application)
    other = application.runtime.create_session(title="other")
    async with run_ready_app(app) as pilot:
        reached, release = asyncio.Event(), asyncio.Event()
        flush = app._flush_input_drafts

        async def delayed():
            reached.set()
            await release.wait()
            await flush()

        with monkeypatch.context() as patch:
            patch.setattr(app, "_flush_input_drafts", delayed)
            composer = app.query_one(Composer)
            composer.load_text("welcome submission")
            await pilot.press("enter")
            await asyncio.wait_for(reached.wait(), 5)
            await app._open_session(other.session_id)
            release.set()
            workers = [w for w in app.workers if w.group == "run"]
            if workers:
                await asyncio.wait_for(app.workers.wait_for_complete(workers), 10)
        assert app._input_drafts.welcome.text == "welcome submission"
        assert application.runtime.store.load_runtime_session(other.session_id).messages == []
        assert len(application.runtime.store.list_sessions()) == 1


@pytest.mark.parametrize("existing", (False, True))
@pytest.mark.parametrize("kind", ("upload", "reference"))
async def test_both_pages_keep_files_in_original_draft_during_navigation(
    tmp_path, existing, kind, monkeypatch
):
    root = tmp_path / "home"
    source = tmp_path / "upload.txt"
    source.write_text("captured input")
    application = application_at(root)
    origin = application.runtime.create_session().session_id if existing else None
    other = application.runtime.create_session(title="other")
    app = OuroTuiApp(application=application, resume_session_id=origin)
    async with run_ready_app(app):
        reached, release = asyncio.Event(), asyncio.Event()
        operation = "import_attachment" if kind == "upload" else "reference_files"
        prepare_files = getattr(app._controller, operation)

        async def delayed(*args):
            reached.set()
            await release.wait()
            return await prepare_files(*args)

        monkeypatch.setattr(app._controller, operation, delayed)
        worker = (
            app._attach_files((source,)) if kind == "upload" else app._reference_files((source,))
        )
        await asyncio.wait_for(reached.wait(), 5)
        await app._open_session(other.session_id)
        release.set()
        await worker.wait()
        saved = TuiInputDraftStore(root).load()
        assert saved.content_for(origin).attachments[0].name == source.name
        assert saved.content_for(other.session_id).is_empty
        assert app.query_one(Composer).input_content.is_empty
        assert len(application.runtime.store.list_sessions()) == 1 + int(existing)


async def test_failed_start_preserves_prepared_draft_on_close(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    identity = application.runtime.resources.reserve()
    drafts = TuiInputDrafts(welcome_resource_id=identity, welcome=InputContent(text="retain"))
    TuiInputDraftStore(tmp_path).save(drafts)
    app = OuroTuiApp(application=application)

    async def failed_start():
        raise ConfigError("startup unavailable")

    monkeypatch.setattr(app._controller, "start", failed_start)
    async with run_ready_app(app):
        assert app.query_one(Composer).disabled
    assert TuiInputDraftStore(tmp_path).load() == drafts
    assert application.runtime.resources.prepared_references(identity) == []


async def test_cancelled_submission_does_not_cancel_its_durable_draft_save(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        reached, release = threading.Event(), threading.Event()
        save = app._input_draft_store.save

        def delayed(drafts):
            reached.set()
            assert release.wait(5)
            save(drafts)

        monkeypatch.setattr(app._input_draft_store, "save", delayed)
        app.query_one(Composer).load_text("interrupted before acceptance")
        await pilot.press("enter")
        assert await asyncio.to_thread(reached.wait, 5)
        workers = [worker for worker in app.workers if worker.group == "run"]
        assert len(workers) == 1
        workers[0].cancel()
        await pilot.pause()
        release.set()
        await app._flush_input_drafts()
        saved = TuiInputDraftStore(tmp_path).load()
        assert len(saved.pending) == 1
        assert saved.pending[0].text == "interrupted before acceptance"
        assert application.runtime.store.list_sessions() == []


@pytest.mark.parametrize("backend", ("files", "sqlite"))
@pytest.mark.parametrize("existing", (False, True))
@pytest.mark.parametrize("accepted", (False, True))
async def test_both_pages_recover_pending_submission_after_restart(
    tmp_path, backend, existing, accepted
):
    root = tmp_path / "home"
    application = application_at(root, backend=backend)
    runtime = application.runtime
    origin = runtime.create_session().session_id if existing else None
    identity = origin or runtime.resources.reserve()
    pending = TuiPendingInput(
        input_id="pending-at-exit",
        session_id=identity,
        origin_session_id=origin,
        text="restore exactly once",
    )
    drafts = TuiInputDrafts(
        welcome_resource_id=identity if origin is None else None, pending=(pending,)
    )
    TuiInputDraftStore(root).save(drafts)
    if accepted:
        await runtime.run(
            pending.text,
            session_id=origin,
            new_session_id=identity if origin is None else None,
            input_id=pending.input_id,
        )
    await application.close()
    restarted_application = application_at(root, backend=backend)
    app = OuroTuiApp(application=restarted_application, resume_session_id=origin)
    async with run_ready_app(app):
        content = app.query_one(Composer).input_content
        assert content.text == ("" if accepted else pending.text)
        assert app._input_drafts.pending == ()
        assert app._controller.session_id == (identity if accepted else origin)
        sessions = restarted_application.runtime.store.list_sessions()
        assert len(sessions) == int(existing or accepted)
        if accepted:
            messages = restarted_application.runtime.store.load_messages_tail(identity)
            assert sum(m.text_content == pending.text for m in messages if m.role == "user") == 1


@pytest.mark.parametrize("existing", (False, True))
@pytest.mark.parametrize("kind", ("upload", "reference"))
async def test_both_pages_delete_and_undo_restored_file_elements(tmp_path, existing, kind):
    root = tmp_path / "home"
    source = tmp_path / "notes.txt"
    source.write_text("leave original untouched")
    application = application_at(root)
    origin = application.runtime.create_session().session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    async with run_ready_app(app):
        if kind == "upload":
            await app._attach_files((source,)).wait()
        else:
            await app._reference_files((source,)).wait()
        original = app.query_one(Composer).input_content
    restarted_application = application_at(root)
    restarted = OuroTuiApp(application=restarted_application, resume_session_id=origin)
    async with run_ready_app(restarted) as pilot:
        composer = restarted.query_one(Composer)
        assert composer.input_content == original
        composer.move_cursor((0, 0))
        await pilot.press("delete")
        assert composer.input_content.is_empty
        composer.undo()
        assert composer.input_content == original
        composer.redo()
        await pilot.pause()
        await restarted._flush_input_drafts()
        assert TuiInputDraftStore(root).load().content_for(origin).is_empty
        assert len(restarted_application.runtime.store.list_sessions()) == int(existing)
        assert source.read_text() == "leave original untouched"
