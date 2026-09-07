import asyncio

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.events import EventRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from ouro.tui.app import OuroTuiApp, _active_stream_tail
from ouro.tui.widgets import Composer, ConversationView
from tests.tui.helpers import run_ready_app


def application_at(tmp_path, provider=None, *, backend="files"):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider or FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment.local(
            tmp_path, store.sessions_dir if backend == "files" else None
        ),
    )
    return LocalApplication.from_runtime(runtime)


async def command(app, pilot, value):
    async with asyncio.timeout(10):
        startup = tuple(worker for worker in app.workers if worker.name == "tui-start")
        if startup:
            await app.workers.wait_for_complete(startup)
    composer = app.query_one(Composer)
    assert not composer.disabled
    composer.load_text(value)
    composer.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    async with asyncio.timeout(10):
        workers = tuple(worker for worker in app.workers if worker.group == "session-history")
        if workers:
            await app.workers.wait_for_complete(workers)
    await pilot.pause()


async def test_undo_returns_latest_user_message_to_composer_and_edit_stays_in_session(tmp_path):
    application = application_at(tmp_path)
    first = await application.runtime.run("first question")
    second = await application.runtime.run("second question", session_id=first.session.session_id)
    app = OuroTuiApp(application=application, resume_session_id=first.session.session_id)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await command(app, pilot, "/undo")
        assert app.query_one(Composer).text == "second question"
        assert app._controller.session_id == first.session.session_id
        assert app.query_one(ConversationView).user_turn_summaries() == ("first question",)
        messages = application.runtime.store.load_runtime_session(first.session.session_id).messages
        assert all(message.run_id != second.run.run_id for message in messages)
        app.query_one(Composer).load_text("edited question")
        await pilot.press("enter")
        await pilot.pause()
        async with asyncio.timeout(10):
            workers = tuple(w for w in app.workers if w.group == "run")
            if workers:
                await app.workers.wait_for_complete(workers)
        messages = application.runtime.store.load_runtime_session(first.session.session_id).messages
        assert [message.text_content for message in messages if message.role == "user"] == [
            "first question",
            "edited question",
        ]


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_abort_undo_and_resubmit_equal_length_input_completes_in_tui(tmp_path, backend):
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
            if workers:
                await app.workers.wait_for_complete(workers)
        assert app._projection.activity.status == "aborted"
        session_id = app._controller.session_id
        await command(app, pilot, "/undo")
        assert app.query_one(Composer).text == "first question"
        assert app.query_one(ConversationView).user_turn_summaries() == ()
        await command(app, pilot, "other question")
        async with asyncio.timeout(10):
            workers = tuple(w for w in app.workers if w.group == "run")
            if workers:
                await app.workers.wait_for_complete(workers)
        await pilot.pause()
        assert app._controller.session_id == session_id
        assert app._projection.activity.status == "completed"
        assert app.query_one(ConversationView).user_turn_summaries() == ("other question",)
        assert not app.query_one(Composer).disabled


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
        await command(app, pilot, "/undo")
        await command(app, pilot, "/undo")
        assert "no user message" in warnings[-1][0]
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
        snapshot = runtime.store.load_runtime_session(result.session.session_id)
        assert len(snapshot.runs) == 1
        assert snapshot.runs[0].run_id == result.run.run_id
        assert snapshot.runs[0].status == "completed"
        assert [m.text_content for m in snapshot.messages if m.role == "user"] == [
            "original question"
        ]


async def test_undo_after_repeated_partial_failure_restores_editable_input(tmp_path):
    class PartialProvider(FauxProvider):
        async def stream(self, **kwargs):
            yield ProviderStreamEvent(type="text_delta", text="partial reply")
            raise RuntimeError("stream disconnected")

    application = application_at(tmp_path, PartialProvider())
    first = await application.runtime.run("first question")
    await application.runtime.run("continue", session_id=first.session.session_id)
    app = OuroTuiApp(application=application, resume_session_id=first.session.session_id)
    async with app.run_test(size=(100, 32)) as pilot:
        await command(app, pilot, "/undo")
        assert app.query_one(Composer).text == "continue"
        assert not app.query_one(Composer).disabled
        assert app.query_one(ConversationView).user_turn_summaries() == ("first question",)
        snapshot = application.runtime.store.load_runtime_session(first.session.session_id)
        assert len(snapshot.messages) == 2
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
        assert "model-round budget" in warnings[-1][0]
        assert warnings[-1][1]["severity"] == "warning"
        assert len(runtime.store.load_runs(original.session.session_id)) == 1


def test_resumed_stream_replay_does_not_restore_withdrawn_partial_text():
    events = [
        EventRecord(
            session_id="ses_resume",
            run_id="run_resume",
            sequence=index + 1,
            event_type=kind,
            payload=payload,
        )
        for index, (kind, payload) in enumerate(
            [
                ("model_text_delta", {"text": "withdrawn partial"}),
                ("run_failed", {"error": "interrupted"}),
                ("run_status_changed", {"to_status": "running", "resumed": True}),
                ("model_text_delta", {"text": "current attempt"}),
            ]
        )
    ]
    assert _active_stream_tail(events) == ("", "current attempt")
