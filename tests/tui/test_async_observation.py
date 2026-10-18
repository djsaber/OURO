import asyncio
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation_history import ConversationHistory
from ouro.tui.widgets import Composer
from tests.application.test_session_event_following import append
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import _application
from tests.tui.test_tasks import streaming_application


async def test_session_observation_does_not_reserve_sqlite_writer(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, None, backend="sqlite")
    store = application.runtime.store
    session = application.runtime.sessions.create(title="Observed").session
    other = application.runtime.sessions.create(title="Independent writer").session
    original = store.load_compactions
    written = []

    def read_with_concurrent_write(session_id):
        snapshot = original(session_id)
        # A second connection must be able to commit while the Host's read is open.
        with closing(sqlite3.connect(store.database, timeout=0)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE sessions SET payload = payload WHERE session_id = ?",
                    (other.session_id,),
                )
        written.append(other.session_id)
        return snapshot

    app = OuroTuiApp(application=application)
    async with run_ready_app(app):
        monkeypatch.setattr(store, "load_compactions", read_with_concurrent_write)
        observation = await app._controller.observe_session(session.session_id)
        try:
            assert observation.loaded.detail.session.session_id == session.session_id
            assert written == [other.session_id]
        finally:
            observation.subscription.close()


async def test_switching_away_closes_tree_that_finishes_subscribing_late(tmp_path, monkeypatch):
    app = OuroTuiApp(application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    tree = SimpleNamespace(close=lambda: closed.append(True))

    async def delayed_subscribe(root_session_id):
        entered.set()
        await release.wait()
        return tree

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "subscribe_agent_tree", delayed_subscribe)
        opening = asyncio.create_task(app._replace_agent_tree("old-root"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await app._replace_agent_tree(None)
        finally:
            release.set()
        assert await opening == ()
        assert closed == [True]
        assert app._agent_tree_subscription is None


async def test_cancelled_session_installation_releases_its_live_subscription(tmp_path, monkeypatch):
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Cancelled attachment").session
    app = OuroTuiApp(application=application)
    entered = asyncio.Event()
    subscriptions = []

    async def delayed_install(loaded, *, subscription, revision, selection=None):
        subscriptions.append(subscription)
        entered.set()
        await asyncio.Event().wait()

    async with run_ready_app(app, size=(100, 30)):
        monkeypatch.setattr(app, "_install_session_surface", delayed_install)
        opening = asyncio.create_task(app._open_session(session.session_id))
        await asyncio.wait_for(entered.wait(), 3)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert subscriptions[0].closed
        await asyncio.gather(*application.supervisor._event_stream_tasks)
        assert not application.supervisor._event_streams


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_body_application_acknowledges_pages_after_apply_and_recovers_lag(
    tmp_path, monkeypatch, backend
):
    application = streaming_application(tmp_path, None, backend=backend)
    session = application.runtime.sessions.create(title="Slow visible consumer").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    applied = []
    async with run_ready_app(app) as pilot:
        observation = app._observation_subscription
        assert observation is not None
        original = app._apply_runtime_event_batch

        async def held_apply(events, *, input_messages):
            if not entered.is_set():
                entered.set()
                await release.wait()
            await original(events, input_messages=input_messages)
            applied.extend(event.sequence for event in events)

        monkeypatch.setattr(app, "_apply_runtime_event_batch", held_apply)
        before = observation.applied_sequence
        first = append(application.runtime.store, session.session_id, "first,")
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for index in range(400):
                last = append(application.runtime.store, session.session_id, f"{index},")
            assert observation.applied_sequence == before
            assert len(app._pending_runtime_events) <= 128
            # The frame worker may be blocked; Composer still owns keyboard input.
            await pilot.press("x")
            assert app.query_one(Composer).text == "x"
        finally:
            release.set()
        async with asyncio.timeout(15):
            while observation.applied_sequence < last.sequence:
                await pilot.pause()
        assert applied == list(range(first.sequence, last.sequence + 1))
        assert app._projection.assistant_text == "first," + "".join(
            f"{index}," for index in range(400)
        )


async def test_late_prepared_selection_cannot_replace_the_newer_view(tmp_path, monkeypatch):
    application = _application(tmp_path)
    first, second, third = (
        application.runtime.sessions.create(title=title).session for title in ("A", "B", "C")
    )
    app = OuroTuiApp(application=application, resume_session_id=first.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    prepared = []
    async with run_ready_app(app):
        original = app._controller.prepare_session_selection

        async def held_selection(session_id):
            result = await original(session_id)
            if session_id == second.session_id:
                prepared.append(result)
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(app._controller, "prepare_session_selection", held_selection)
        opening = asyncio.create_task(app._open_session(second.session_id))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await app._open_session(third.session_id)
            app.query_one(Composer).load_text("C draft")
        finally:
            release.set()
        await opening
        assert app._controller.session_id == third.session_id
        assert app._projection.session_id == third.session_id
        assert app.query_one(Composer).text == "C draft"
        assert prepared[0].subscription.closed
        await app._open_session(first.session_id)
        assert app._projection.session_id == first.session_id


async def test_history_render_does_not_hold_the_content_cursor(tmp_path, monkeypatch):
    import ouro.tui.conversation_history as history_module
    from ouro.core.messages import MessageRecord

    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Render independently").session
    application.runtime.store.append_messages(
        [
            MessageRecord.text(
                session_id=session.session_id,
                sequence=index + 1,
                role="user" if index % 2 == 0 else "assistant",
                text=f"history {index}",
            )
            for index in range(160)
        ]
    )
    app = OuroTuiApp(application=application)
    entered, release = threading.Event(), threading.Event()
    original = history_module._render_entry_requests

    def held_render(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    async with run_ready_app(app) as pilot:
        monkeypatch.setattr(history_module, "_render_entry_requests", held_render)
        try:
            await app._open_session(session.session_id)
            assert await asyncio.to_thread(entered.wait, 5)
            observation = app._observation_subscription
            last = append(application.runtime.store, session.session_id, "new tail")
            async with asyncio.timeout(5):
                while observation.applied_sequence < last.sequence:
                    await pilot.pause()
            assert app._projection.assistant_text == "new tail"
            assert app.query_one(ConversationHistory)._rendered_count == 0
            await pilot.press("a")
            assert app.query_one(Composer).text == "a"
        finally:
            release.set()
