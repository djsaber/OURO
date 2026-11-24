import asyncio

import pytest
from textual.worker import WorkerCancelled

from ouro.application.inputs.skills import SkillInvocationRequest
from ouro.application.resources import ResourceService
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.input_content import InputAttachment, InputContent
from ouro.core.messages import MessageRecord, SkillActivationContent
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.resources import LocalResourceStore
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.skills import discover_skill_catalog
from ouro.tools import ToolRegistry
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.drafts import TuiInputDraftStore
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.view import ConversationView
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from tests.application.test_skill_activations import CapturingProvider
from tests.run_facts import run_events
from tests.tui.helpers import run_ready_app, wait_for_workers, wait_until


def application_at(tmp_path, provider=None, *, backend="files", skills=None):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    resources = LocalResourceStore(tmp_path)
    resources.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider or FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")
        ),
        tools=ToolRegistry(),
        resources=ResourceService(store=resources, sessions=store),
        skills=tuple(discover_skill_catalog(home=tmp_path).skills) if skills is None else skills,
        skill_catalog_resolver=(
            (lambda: tuple(discover_skill_catalog(home=tmp_path).skills))
            if skills is None
            else None
        ),
        environment=RuntimeEnvironment.local(tmp_path),
    )
    return LocalApplication.from_runtime(runtime)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_edit_skill_and_attachment_survive_navigation_restart_and_submission(
    tmp_path, backend
):
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: Demo.\n---\nUse this workflow.\n", encoding="utf-8"
    )
    application = application_at(tmp_path, backend=backend)
    runtime = application.runtime
    session = runtime.create_session(title="rich input")
    resource_file = tmp_path / "notes.txt"
    resource_file.write_text("reference details", encoding="utf-8")
    resource = runtime.resources.import_resource(session.session_id, resource_file).resource
    original = await runtime.run(
        "original",
        session_id=session.session_id,
        skill_invocation=SkillInvocationRequest(name="demo", arguments="original"),
        resource_refs=(resource.resource_id,),
    )
    other = runtime.create_session(title="other")
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        await command(app, pilot, "/edit #1")
        composer = app.query_one(Composer)
        assert composer.input_text == ""
        await pilot.press(*"edited")
        await app._open_session(other.session_id)
        assert composer.context_element is None
        assert app._input_drafts.resources_for(other.session_id) == ()
        await app._open_session(session.session_id)
        assert composer.input_text == "edited" and "Edit #1" in composer.text
        assert app._input_drafts.resources_for(session.session_id) == (resource.resource_id,)
    assert TuiInputDraftStore(tmp_path).load().resources_for(session.session_id) == (
        resource.resource_id,
    )
    restarted_application = application_at(tmp_path, backend=backend)
    restarted = OuroTuiApp(application=restarted_application, resume_session_id=session.session_id)
    async with run_ready_app(restarted, size=(100, 32)) as pilot:
        composer = restarted.query_one(Composer)
        assert composer.input_text == "edited" and "Edit #1" in composer.text
        assert [item.name for item in composer.input_content.skills] == ["demo"]
        await pilot.press("enter")
        async with asyncio.timeout(10):
            workers = tuple(w for w in restarted.workers if w.group == "run")
            if workers:
                await wait_for_workers(restarted, workers)
        messages = restarted_application.runtime.store.load_messages_tail(session.session_id)
        user = next(message for message in messages if message.role == "user")
        assert user.text_content == "edited"
        assert user.input_attachments[0].resource_id == resource.resource_id
        assert next(
            message for message in messages if message.semantic_tag == "attachments"
        ).resource_refs == [resource.resource_id]
        assert any(
            isinstance(block, SkillActivationContent) and block.name == "demo"
            for block in user.content
        )
        assert user.run_id != original.run.run_id


async def command(app, pilot, value, *, attachments=(), skills=()):
    async with asyncio.timeout(10):
        startup = tuple(worker for worker in app.workers if worker.name == "tui-start")
        if startup:
            await wait_for_workers(app, startup)
    composer = app.query_one(Composer)
    # Runtime admission may precede the new session surface becoming editable.
    async with asyncio.timeout(10):
        while composer.disabled:
            await pilot.pause()
    assert not composer.disabled
    composer.load_input(
        InputContent(
            text=value,
            skills=tuple(
                item.model_copy(update={"text_offset": min(item.text_offset, len(value))})
                for item in skills
            ),
            attachments=tuple(
                item.model_copy(update={"text_offset": len(value)}) for item in attachments
            ),
        ),
        context_element=composer.context_element,
    )
    composer.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    async with asyncio.timeout(10):
        workers = tuple(
            worker
            for worker in app.workers
            if worker.group in {"composer-command", "session-history"}
        )
        if workers:
            await wait_for_workers(app, workers)
    await pilot.pause()


@pytest.mark.parametrize("remember", [True, False])
async def test_failed_edit_selection_and_submit_keep_text_and_resources(
    tmp_path, monkeypatch, remember
):
    TuiPreferencesStore(tmp_path).save(TuiPreferences(input_draft_memory_enabled=remember))
    application = application_at(tmp_path)
    first = await application.runtime.run("original")
    app = OuroTuiApp(application=application, resume_session_id=first.session.session_id)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        app._remember_input_draft(
            first.session.session_id,
            "draft",
            attachments=(
                InputAttachment(resource_id="res_selected", name="selected.txt", text_offset=5),
            ),
        )
        app._edit_draft(app._input_drafts.content_for(first.session.session_id))

        async def fail(*args, **kwargs):
            raise PersistenceError("injected failure")

        reading, release = asyncio.Event(), asyncio.Event()

        async def fail_selection(*args):
            reading.set()
            await release.wait()
            await fail()

        monkeypatch.setattr(app._controller, "message", fail_selection)
        selection = asyncio.create_task(app._edit_message("#1"))
        await asyncio.wait_for(reading.wait(), 5)
        try:
            await pilot.press("enter")
            assert app.query_one(Composer).input_content.text == "draft"
        finally:
            release.set()
            await selection
        assert app._input_drafts.resources_for(first.session.session_id) == ("res_selected",)
        monkeypatch.setattr(app._controller, "submit", fail)
        await command(
            app,
            pilot,
            "edited draft",
            attachments=app.query_one(Composer).input_content.attachments,
        )
        async with asyncio.timeout(10):
            workers = tuple(w for w in app.workers if w.group == "run")
            if workers:
                await wait_for_workers(app, workers)
        assert app.query_one(Composer).input_content.text == "edited draft"
        assert app._input_drafts.resources_for(first.session.session_id) == ("res_selected",)
    stored = TuiInputDraftStore(tmp_path).load()
    assert stored.resources_for(first.session.session_id) == (("res_selected",) if remember else ())


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_edit_numbered_message_replaces_suffix_on_submit(tmp_path, monkeypatch, backend):
    provider = CapturingProvider()
    application = application_at(tmp_path, provider=provider, backend=backend)
    first = await application.runtime.run("first question")
    session_id = first.session.session_id
    store = application.runtime.store
    prefix = store.load_session(session_id).messages
    await application.runtime.run("second question", session_id=session_id)
    third = await application.runtime.run("third question", session_id=session_id)
    before = store.load_session(session_id).messages
    cuts = []
    commit = store.commit_session

    def capture_cut(request):
        if request.before_message_id:
            cuts.append((request.before_message_id, [r.item.prompt for r in request.inputs]))
        return commit(request)

    monkeypatch.setattr(store, "commit_session", capture_cut)
    app = OuroTuiApp(application=application, resume_session_id=session_id)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.press("slash", *"edit", "enter")
        popup = app.query_one(CommandPopup)
        assert [option.value for option in popup._arguments] == ["#1", "#2", "#3"]
        await pilot.press("down", "down", "enter")
        composer = app.query_one(Composer)
        await wait_until(
            lambda: "Edit #3「third question」" in composer.text, pilot, description="edit target"
        )
        assert store.load_session(session_id).messages == before
        assert cuts == []
        assert composer.input_content.is_empty
        await pilot.press(*"edited", "space", *"question")
        await pilot.press("ctrl+home", "slash", *"edit", "space", "#", "2", "enter")
        await wait_until(
            lambda: "Edit #2「second question」" in composer.text,
            pilot,
            description="reselected target",
        )
        await pilot.press("ctrl+home", "slash", *"edit", "enter", "up", "escape")
        assert composer.input_content == InputContent(text="edited question")
        assert composer.text.count("Edit #") == 1
        assert "Edit #2「second question」" in composer.text
        await pilot.press("enter")
        await wait_until(
            lambda: (
                app.query_one(ConversationView).user_turn_summaries()
                == ("first question", "edited question")
                and app._projection.activity.status == "completed"
            ),
            pilot,
            description="edited run completed",
        )
        after = store.load_session(session_id)
        assert app._controller.session_id == session_id
        assert after.messages[: len(prefix)] == prefix
        assert after.session.history_epoch == 1
        assert cuts == [(before[len(prefix)].message_id, ["edited question"])]
        assert [m.text_content for m in provider.calls[-1] if m.role == "user"] == [
            "first question",
            "edited question",
        ]
        assert {m.message_id for m in after.messages}.isdisjoint(
            m.message_id for m in before[len(prefix) :]
        )
        await app._apply_runtime_event_batch(run_events(store, third), input_messages={})
        assert app.query_one(ConversationView).user_turn_summaries() == (
            "first question",
            "edited question",
        )


async def test_removing_edit_target_keeps_files_and_submits_a_new_message(tmp_path):
    application = application_at(tmp_path)
    runtime = application.runtime
    session = runtime.create_session()
    path = tmp_path / "notes.txt"
    path.write_text("notes", encoding="utf-8")
    resource = runtime.resources.import_resource(session.session_id, path).resource
    await runtime.run(
        "original", session_id=session.session_id, resource_refs=(resource.resource_id,)
    )
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        await command(app, pilot, "/edit #1")
        composer = app.query_one(Composer)
        await pilot.press(*"next", "down", "home", "delete")
        assert "Edit #1" not in composer.text
        assert composer.input_text == "next"
        assert composer.input_content.resource_refs == (resource.resource_id,)
        await pilot.press("enter")
        await wait_until(
            lambda: len(runtime.store.load_runs(session.session_id)) == 2,
            pilot,
            description="new message accepted",
        )
        after = runtime.store.load_session(session.session_id)
        assert after.session.history_epoch == 0
        users = [message for message in after.messages if message.role == "user"]
        assert [message.text_content for message in users] == ["original", "next"]
        assert users[-1].input_attachments[0].resource_id == resource.resource_id


async def test_failed_edit_can_retry_or_cancel_to_previous_draft(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    runtime = application.runtime
    first = await runtime.run("original")
    session_id = first.session.session_id
    before = runtime.store.load_session(session_id)
    app = OuroTuiApp(application=application, resume_session_id=session_id)
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("unsent follow-up")
        await pilot.pause()
        await app._edit_message("#1")
        assert "Edit #1「original」" in composer.text

        async def fail(*args, **kwargs):
            raise PersistenceError("not accepted")

        monkeypatch.setattr(app._controller, "submit", fail)
        await command(app, pilot, "updated")
        await wait_until(
            lambda: composer.input_text == "updated" and "Edit #1「original」" in composer.text,
            pilot,
            description="rejected edit restored",
        )
        assert (
            app._input_drafts.context_for(session_id).target.message_id
            == before.messages[0].message_id
        )
        await pilot.press("ctrl+end", "backspace")
        assert composer.text == "updated"
        composer.undo()
        await pilot.pause()
        assert "Edit #1「original」" in composer.text
        await pilot.press("escape")
        assert composer.text == "unsent follow-up"
        assert app._input_drafts.context_for(session_id) is None
        assert runtime.store.load_session(session_id) == before


async def test_submitted_edit_keeps_its_target_while_waiting_for_a_command(tmp_path):
    application = application_at(tmp_path)
    runtime = application.runtime
    session_id = (await runtime.run("original")).session.session_id
    app = OuroTuiApp(application=application, resume_session_id=session_id)
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("unsent follow-up")
        await pilot.pause()
        await app._edit_message("#1")
        async with app._composer_submission_lock:
            composer.insert("replacement", (0, 0))
            await pilot.press("enter", "escape")
            assert composer.text == "unsent follow-up"
        await wait_until(
            lambda: app.query_one(ConversationView).user_turn_summaries() == ("replacement",),
            pilot,
            description="submitted edit replaced the selected input",
        )
        assert runtime.store.load_session_record(session_id).history_epoch == 1


async def test_numbered_edit_waiting_on_a_command_cannot_select_another_session(tmp_path):
    application = application_at(tmp_path)
    runtime = application.runtime
    first = (await runtime.run("first session")).session
    other = (await runtime.run("other session")).session
    app = OuroTuiApp(application=application, resume_session_id=first.session_id)
    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        async with app._composer_submission_lock:
            composer.load_text("/edit #1")
            await pilot.pause()
            await pilot.press("enter")
            commands = tuple(w for w in app.workers if w.group == "composer-command")
            assert commands
            await app._open_session(other.session_id)
        await wait_for_workers(app, commands)
        assert app._input_drafts.context_for(other.session_id) is None
        assert composer.text == ""


async def test_edit_loads_global_numbers_and_preserves_direct_input_identity(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    runtime = application.runtime
    session = runtime.create_session()
    messages = [
        MessageRecord.text(
            session_id=session.session_id, role="user", text=f"message {n}"
        ).model_copy(
            update={"metadata": {"session_input_source": "user" if n in (1, 205) else "host"}}
        )
        for n in range(1, 206)
    ]
    runtime.store.append_messages(messages)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        assert app._history_before_cursor is not None
        await command(app, pilot, "/edit #1")
        assert "Edit #1「message 1」" in app.query_one(Composer).text
        assert app._history_before_cursor is None
        assert [arg.value for arg in app._user_message_arguments(direct_only=True)] == [
            "#1",
            "#205",
        ]
        await pilot.press("escape")
        warnings = []
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: warnings.append(str(message)))
        await command(app, pilot, "/edit #2")
        assert "Choose a message you sent" in warnings[-1]
        assert app._input_drafts.context_for(session.session_id) is None
        assert runtime.store.load_session_record(session.session_id).history_epoch == 0


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_tui_message_query_reloads_if_history_changes_during_event_delivery(
    tmp_path, backend, monkeypatch
):
    application = application_at(tmp_path, backend=backend)
    runtime = application.runtime
    first = await runtime.run("keep")
    session_id = first.session.session_id
    app = OuroTuiApp(application=application, resume_session_id=session_id)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        session = runtime.store.load_session_record(session_id)
        message = MessageRecord.text(session_id=session_id, role="user", text="withdraw")
        added = runtime.store.commit_session(
            SessionCommit(before=session, steps=(SessionCommitStep(message=message),))
        )
        read = runtime.sessions.messages_at_history

        def withdraw_before_read(*args, **kwargs):
            runtime.store.commit_session(
                SessionCommit(before=added.session, before_message_id=message.message_id)
            )
            return read(*args, **kwargs)

        monkeypatch.setattr(runtime.sessions, "messages_at_history", withdraw_before_read)
        event = EventRecord(
            session_id=session_id,
            sequence=app._projection.last_sequence + 1,
            event_type="user_message_added",
            payload={"message_id": message.message_id},
        )
        await application.supervisor._on_event(event)
        async with asyncio.timeout(8):
            while app._projection.history_epoch != 1:
                await pilot.pause()
        assert app._projection.history_epoch == 1
        assert app._projection.activity.activity == "idle"
        assert app.query_one(ConversationView).user_turn_summaries() == ("keep",)
        monkeypatch.setattr(runtime.sessions, "messages_at_history", read)
        # The same missing identity within the current epoch is data loss, not lag.
        failures = []
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: failures.append(message))
        await application.supervisor._on_event(
            EventRecord(
                session_id=session_id,
                history_epoch=1,
                event_type="user_message_added",
                payload={"message_id": message.message_id},
            )
        )
        async with asyncio.timeout(8):
            while not failures:
                await pilot.pause()
        assert app._observation_subscription.closed
        assert any("PersistenceError" in message for message in failures)
        assert app.query_one(ConversationView).user_turn_summaries() == ("keep",)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("edited", ["other question", "a longer replacement question"])
async def test_abort_edit_resubmit_and_restart_in_tui(tmp_path, backend, edited):
    class InterruptedProvider(FauxProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def stream(self, **kwargs):
            if not self.started.is_set():
                self.started.set()
                await asyncio.Event().wait()
            async for event in super().stream(**kwargs):
                yield event

    provider = InterruptedProvider()
    application = application_at(tmp_path, provider, backend=backend)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        await command(app, pilot, "first question")
        await asyncio.wait_for(provider.started.wait(), 5)
        await command(app, pilot, "/abort")
        async with asyncio.timeout(10):
            workers = tuple(w for w in app.workers if w.group in {"abort", "run"})
            results = await asyncio.gather(
                *(worker.wait() for worker in workers), return_exceptions=True
            )
            assert all(result is None or isinstance(result, WorkerCancelled) for result in results)
            while app._projection is None or app._projection.activity.status != "aborted":
                await pilot.pause()
        assert app._projection.activity.status == "aborted"
        session_id = app._controller.session_id
        original = application.runtime.store.load_messages_tail(session_id)[0]
        await command(app, pilot, "/edit #1")
        assert "Edit #1「first question」" in app.query_one(Composer).text
        assert app.query_one(ConversationView).user_turn_summaries() == ("first question",)
        # A delayed input notification must not reload the withdrawn message.
        late = EventRecord.model_validate(
            EventRecord(
                session_id=session_id,
                run_id=original.run_id,
                history_epoch=0,
                event_type="user_message_added",
                payload={"message_id": original.message_id},
            )
        )
        await command(app, pilot, edited)
        async with asyncio.timeout(10):
            workers = tuple(w for w in app.workers if w.group == "run")
            if workers:
                await wait_for_workers(app, workers)
        await pilot.pause()
        await app._apply_runtime_event_batch((late,), input_messages={})
        assert app._projection.history_epoch == 1
        assert app._controller.session_id == session_id
        assert app._projection.activity.status == "completed"
        assert app.query_one(ConversationView).user_turn_summaries() == (edited,)
        assert not app.query_one(Composer).disabled

    restarted = OuroTuiApp(
        application=application_at(tmp_path, backend=backend), resume_session_id=session_id
    )
    async with run_ready_app(restarted, size=(100, 32)):
        assert restarted._projection.history_epoch == 1
        assert restarted._projection.activity.status == "completed"
        assert restarted.query_one(ConversationView).user_turn_summaries() == (edited,)
        assert not restarted.query_one(Composer).disabled


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_open_session_reconciles_interrupted_run_but_preserves_live_owner(
    tmp_path, live, backend
):
    application = application_at(tmp_path, backend=backend)
    store = application.runtime.store
    run_id = "run_interrupted"
    session = SessionRecord(status="running", active_run_id=run_id, last_run_id=run_id)
    store.create_session(session)
    store.save_run(RunRecord(session_id=session.session_id, run_id=run_id, status="running"))
    lock = store.acquire_session_lock(session.session_id, run_id) if live else None
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    try:
        async with run_ready_app(app, size=(100, 32)):
            expected = "running" if live else "recoverable"
            assert store.load_run(session.session_id, run_id).status == expected
            assert app._projection.activity.status == expected
            assert app._projection.activity.activity == (
                "preparing_model" if live else "recoverable"
            )
            assert not app.query_one(Composer).disabled
    finally:
        if lock is not None:
            lock.release()


async def test_continue_completed_and_empty_commands_show_warning_without_new_run(
    tmp_path, monkeypatch
):
    application = application_at(tmp_path)
    result = await application.runtime.run("complete this")
    app = OuroTuiApp(application=application, resume_session_id=result.session.session_id)
    warnings = []
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app, "notify", lambda message, **kwargs: warnings.append((str(message), kwargs))
        )
        await command(app, pilot, "/continue")
        assert "completed" in warnings[-1][0]
        assert warnings[-1][1]["severity"] == "warning"
        assert len(application.runtime.store.load_runs(result.session.session_id)) == 1
        await command(app, pilot, "/edit #2")
        assert "no message numbered #2" in warnings[-1][0]
        assert warnings[-1][1]["severity"] == "warning"
        assert not app.query_one(Composer).disabled


@pytest.mark.parametrize("status", ["failed", "paused"])
async def test_continue_interrupted_run_uses_command_without_a_user_message(tmp_path, status):
    application = application_at(
        tmp_path,
        FauxProvider(
            event_batches=[
                [],
                [
                    ProviderStreamEvent(type="text_delta", text="continued"),
                    ProviderStreamEvent(type="done"),
                ],
            ]
        ),
    )
    runtime = application.runtime
    result = await runtime.run("original question")
    assert result.run.status == "failed"
    runtime.store.save_run(result.run.model_copy(update={"status": status}))
    app = OuroTuiApp(application=application, resume_session_id=result.session.session_id)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await command(app, pilot, "/continue")
        snapshot = runtime.store.load_session(result.session.session_id)
        assert len(snapshot.runs) == 1
        assert snapshot.runs[0].run_id == result.run.run_id
        assert snapshot.runs[0].status == "completed"
        assert [m.text_content for m in snapshot.messages if m.role == "user"] == [
            "original question"
        ]
        history = app.query_one(ConversationHistory)
        await wait_until(
            lambda: any(entry.run and entry.run.status == "completed" for entry in history.entries),
            pilot,
            description="continued Run summary",
        )
        assert [
            (entry.kind, entry.text)
            for entry in history.entries
            if entry.run_id == result.run.run_id
        ] == [
            ("user", "original question"),
            ("assistant", "continued"),
            ("run", ""),
        ]


async def test_edit_after_repeated_partial_failure_restores_editable_input(tmp_path):
    class PartialProvider(FauxProvider):
        async def stream(self, **kwargs):
            yield ProviderStreamEvent(type="text_delta", text="partial reply")
            raise RuntimeError("stream disconnected")

    application = application_at(tmp_path, PartialProvider())
    first = await application.runtime.run("first question")
    await application.runtime.run("continue", session_id=first.session.session_id)
    app = OuroTuiApp(application=application, resume_session_id=first.session.session_id)
    async with app.run_test(size=(100, 32)) as pilot:
        await command(app, pilot, "/edit #2")
        assert "Edit #2「continue」" in app.query_one(Composer).text
        assert not app.query_one(Composer).disabled
        assert app.query_one(ConversationView).user_turn_summaries() == (
            "first question",
            "continue",
        )
        snapshot = application.runtime.store.load_session(first.session.session_id)
        assert len(snapshot.messages) == 4
        assert snapshot.messages[-1].metadata["incomplete"]


async def test_continue_respects_remaining_budget_and_explains_pause(tmp_path, monkeypatch):
    application = application_at(tmp_path)
    runtime = application.runtime
    original = await runtime.run("original task")
    metadata = dict(original.run.metadata)
    metadata["run_budget"] = {"max_model_rounds": 1}
    runtime.store.save_run(
        original.run.model_copy(update={"status": "paused", "metadata": metadata})
    )
    warnings = []
    app = OuroTuiApp(application=application, resume_session_id=original.session.session_id)
    async with app.run_test(size=(100, 32)) as pilot:
        monkeypatch.setattr(
            app, "notify", lambda message, **kwargs: warnings.append((str(message), kwargs))
        )
        await command(app, pilot, "/continue")
        run = runtime.store.load_run(original.session.session_id, original.run.run_id)
        assert run.status == "paused"
        assert run.model_round_count == original.run.model_round_count
        assert "limit on model calls" in warnings[-1][0]
        assert warnings[-1][1]["severity"] == "warning"
        assert len(runtime.store.load_runs(original.session.session_id)) == 1
