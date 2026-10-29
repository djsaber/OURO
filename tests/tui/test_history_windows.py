from __future__ import annotations

import asyncio
import threading
from datetime import timedelta

import pytest
from textual import events

import ouro.tui.conversation.history as history_module
from ouro.core.base import utc_now
from ouro.core.errors import SessionNotFoundError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.view import ConversationView
from tests.tui.helpers import release_on_exit, run_ready_app, wait_until
from tests.tui.test_background_navigation import NavigationProvider
from tests.tui.test_tasks import streaming_application


def seed(application, count):
    session = application.runtime.sessions.create(title="Windowed history").session
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text=f"message {index}")
        for index in range(count)
    ]
    application.runtime.store.append_messages(messages)
    return session, messages


@pytest.mark.parametrize("long_answer", [False, True])
async def test_first_history_frame_is_the_latest_window_not_a_replay(
    tmp_path, monkeypatch, long_answer
):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 405)
    last_text = "message 404"
    expected_entries = 200
    if long_answer:
        application.runtime.store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                role="assistant",
                text="\n\n".join(f"Answer paragraph {index}" for index in range(400)),
            )
        )
        last_text = "Answer paragraph 399"
        expected_entries = 599
    app = OuroTuiApp(application=application)
    entered, release = threading.Event(), threading.Event()
    render = history_module._render_entry_requests
    calls = 0
    frames = []

    def held_render(requests, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(10)
        return render(requests, **kwargs)

    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        conversation = app.query_one(ConversationView)
        display = app._display

        def record_frame(screen, renderable):
            if renderable is not None and history.lines:
                frames.append(
                    (history.rendered_text, conversation.scroll_y, conversation.max_scroll_y)
                )
            display(screen, renderable)

        monkeypatch.setattr(history_module, "_render_entry_requests", held_render)
        monkeypatch.setattr(app, "_display", record_frame)
        try:
            await app._open_session(session.session_id)
            assert await asyncio.to_thread(entered.wait, 5)
            await pilot.pause()
            assert not history.lines
            await pilot.press("x")
            assert app.query_one(Composer).text == "x"
        finally:
            release.set()
        await history.wait_until_rendered()
        await wait_until(lambda: bool(frames), pilot, description="latest history frame")
        assert frames
        assert all(last_text in text and y == bottom for text, y, bottom in frames)
        assert len(history.entries) == expected_entries
        assert app._history_before_cursor is not None


async def test_view_commit_uses_prepared_policy_and_reasoning_facts(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 3)
    app = OuroTuiApp(application=application)
    reads = []
    async with run_ready_app(app):
        for name in ("session_approval_preset", "session_reasoning"):
            original = getattr(app._controller, name)

            def record(operation, label):
                async def query():
                    reads.append(label)
                    return await operation()

                return query

            monkeypatch.setattr(app._controller, name, record(original, name))
        await app._open_session(session.session_id)
        assert reads == []


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("prefix_before_open", [True, False])
async def test_opening_a_live_session_does_not_read_full_execution_history(
    tmp_path, monkeypatch, backend, prefix_before_open
):
    provider = NavigationProvider()
    prefix_ready, release_prefix = asyncio.Event(), asyncio.Event()
    stream = provider.stream

    async def gated_stream(**kwargs):
        async for event in stream(**kwargs):
            if event.type == "text_delta":
                prefix_ready.set()
                await release_prefix.wait()
            yield event

    monkeypatch.setattr(provider, "stream", gated_stream)
    application = streaming_application(tmp_path, provider, backend=backend)
    session, _messages = seed(application, 405)
    app = OuroTuiApp(application=application, close_application=False)
    try:
        async with run_ready_app(app) as pilot, release_on_exit(release_prefix):
            handle = await application.supervisor.submit("live", session_id=session.session_id)
            await wait_until(prefix_ready.is_set, pilot, description="provider prefix ready")
            if prefix_before_open:
                subscription = await application.supervisor.subscribe(session.session_id)
                try:
                    release_prefix.set()
                    # Provider entry precedes publication. Wait for the durable
                    # delta so this case exercises the observation snapshot.
                    async with asyncio.timeout(15):
                        while True:
                            notification = await subscription.get()
                            assert notification.diagnostic is None, notification.diagnostic
                            if (
                                notification.event is not None
                                and notification.event.event_type == "model_text_delta"
                            ):
                                break
                finally:
                    subscription.close()

            def forbidden(*args, **kwargs):
                pytest.fail("observation of an owned Run loaded full execution history")

            with monkeypatch.context() as patch:
                patch.setattr(application.runtime.store, "load_runtime_session", forbidden)
                await app._open_session(session.session_id)
                assert app._projection.activity.status == "running"
                if not prefix_before_open:
                    assert app._projection.assistant_text == ""
                    release_prefix.set()
                    # The same bounded observation must also receive deltas
                    # published after opening, without a full-history reload.
                    await wait_until(
                        lambda: app._projection.assistant_text == "live prefix. ",
                        pilot,
                        description="live prefix after opening",
                    )
                assert app._projection.assistant_text == "live prefix. "
            provider.finish("live")
            await application.supervisor.wait(handle)
    finally:
        if "live" in provider.streams:
            provider.finish("live")
        await application.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("new_messages", [1, 450])
@pytest.mark.parametrize("destination", ["other", "home"])
async def test_recent_window_pages_and_cached_reentry_keep_the_same_history_start(
    tmp_path, backend, new_messages, destination, monkeypatch
):
    application = streaming_application(tmp_path, None, backend=backend)
    session, messages = seed(application, 605)
    other = application.runtime.sessions.create(title="Other").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        await history.wait_until_rendered()
        assert [entry.text for entry in history.entries] == [
            message.text_content for message in messages[-200:]
        ]
        assert app._history_before_cursor is not None
        await pilot.press("x")
        assert app.query_one(Composer).text == "x"
        assert await app._prepend_history_window(app._view_revision)
        await history.wait_until_rendered()
        await pilot.pause()
        assert [entry.text for entry in history.entries] == [
            message.text_content for message in messages[-400:]
        ]
        conversation = app.query_one(ConversationView)
        conversation.release_anchor()
        conversation.scroll_to(y=100, animate=False)
        await pilot.pause()
        held = conversation.held_scroll_y()
        if destination == "home":
            await app._new_session()
            await pilot.pause()
        else:
            await app._open_session(other.session_id)
        extra = [
            MessageRecord.text(
                session_id=session.session_id, role="user", text=f"new while away {index}"
            )
            for index in range(new_messages)
        ]
        application.runtime.store.append_messages(extra)
        projected = []
        prepare = app._prepare_loaded_session

        def record_projection(loaded, *args, **kwargs):
            projected.extend(message.sequence for message in loaded.messages)
            return prepare(loaded, *args, **kwargs)

        monkeypatch.setattr(app, "_prepare_loaded_session", record_projection)
        rendered = []
        render = history_module._render_entry_requests

        def record_render(requests, **kwargs):
            rendered.extend(request.entry.source_sequence for request in requests)
            return render(requests, **kwargs)

        monkeypatch.setattr(history_module, "_render_entry_requests", record_render)
        await app._open_session(session.session_id)
        await history.wait_until_rendered()
        await pilot.pause()
        assert [entry.text for entry in history.entries] == [
            *(message.text_content for message in messages[-400:]),
            *(message.text_content for message in extra),
        ]
        assert len(projected) == (200 if new_messages == 1 else 600)
        assert min(projected) > 206  # The stable retained prefix was not read/projected again.
        assert all(sequence is not None and sequence > 605 for sequence in rendered)
        assert conversation.held_scroll_y() == held
        assert app.query_one(Composer).text == "x"


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_numbered_navigation_loads_complete_history_before_resolving_numbers(
    tmp_path, backend
):
    application = streaming_application(tmp_path, None, backend=backend)
    session, messages = seed(application, 405)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        app._navigate_to("#1")
        async with asyncio.timeout(15):
            while app._history_loading:
                await pilot.pause()
        assert not app._history_load_failed
        assert app._history_before_cursor is None
        assert [option.value for option in app._user_message_arguments()] == [
            f"#{index}" for index in range(1, 406)
        ]
        history = app.query_one(ConversationHistory)
        assert [entry.text for entry in history.entries] == [
            message.text_content for message in messages
        ]
        await pilot.pause()
        assert app.query_one(ConversationView).scroll_y == history.user_lines[0]


async def test_numbered_navigation_extends_an_inflight_earlier_page(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, None)
    session, messages = seed(application, 605)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    started, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app) as pilot:
        original = app._controller.history_window

        async def held(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app._controller, "history_window", held)
        await app.query_one(ConversationHistory).wait_until_rendered()
        await pilot.pause()
        app.query_one(ConversationView).scroll_to(y=0, animate=False)
        await asyncio.wait_for(started.wait(), 5)
        app._navigate_to("#1")
        release.set()
        async with asyncio.timeout(15):
            while app._history_loading:
                await pilot.pause()
        history = app.query_one(ConversationHistory)
        assert len(history.entries) == len(messages)
        assert app._history_before_cursor is None
        await pilot.pause()
        assert app.query_one(ConversationView).scroll_y == history.user_lines[0]


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("paging", ["page", "arrow"])
async def test_scrolling_to_history_start_loads_one_page_and_preserves_viewport(
    tmp_path, monkeypatch, backend, paging
):
    application = streaming_application(tmp_path, None, backend=backend)
    session, _messages = seed(application, 605)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        conversation = app.query_one(ConversationView)
        await history.wait_until_rendered()
        await pilot.pause()
        assert len(history.entries) == 200
        initial_widgets = set(conversation.query("*"))
        original = app._controller.history_window

        async def held(*args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app._controller, "history_window", held)
        conversation.scroll_to(y=0, animate=False)
        await asyncio.wait_for(entered.wait(), 5)
        try:
            for _ in range(3):
                conversation.post_message(
                    events.MouseScrollUp(conversation, 0, 0, 0, -1, 0, False, False, False)
                )
            await pilot.pause()
            assert calls == 1
            assert len(history.entries) == 200
        finally:
            release.set()
        async with asyncio.timeout(10):
            while app._history_loading:
                await pilot.pause()
        await pilot.pause()
        assert calls == 1
        assert len(history.entries) == 400
        assert conversation.scroll_y == history.user_lines[200]
        assert history.entries[200].text == "message 405"

        conversation.scroll_to(y=1, animate=False)
        await pilot.pause()
        if paging == "page":
            conversation.action_page_up()
        else:
            conversation.action_scroll_up()
        async with asyncio.timeout(10):
            while len(history.entries) != 600 or app._history_loading:
                await pilot.pause()
        await pilot.pause()
        assert calls == 2
        assert conversation.scroll_y == history.user_lines[200]
        assert set(conversation.query("*")) == initial_widgets


async def test_previous_user_navigation_crosses_an_older_page(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 405)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        conversation = app.query_one(ConversationView)
        await history.wait_until_rendered()
        await pilot.pause()
        original = app._controller.history_window

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app._controller, "history_window", held)
        conversation.scroll_to_user_turn(0)
        await asyncio.wait_for(entered.wait(), 5)
        app.action_previous_user_message()
        release.set()
        async with asyncio.timeout(10):
            while app._history_loading:
                await pilot.pause()
        await pilot.pause()
        assert len(history.entries) == 400
        assert history.entries[199].text == "message 204"
        assert conversation.scroll_y == history.user_lines[199]


@pytest.mark.parametrize("target", ["bottom", "previous", "next"])
async def test_local_navigation_supersedes_a_pending_full_history_jump(
    tmp_path, monkeypatch, target
):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 605)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        conversation = app.query_one(ConversationView)
        await history.wait_until_rendered()
        conversation.scroll_to_user_turn(10)
        await pilot.pause()
        original = app._controller.history_window

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app._controller, "history_window", held)
        app._navigate_to("#1")
        await asyncio.wait_for(entered.wait(), 5)
        if target == "bottom":
            app._navigate_to("bottom")
        elif target == "previous":
            app.action_previous_user_message()
        else:
            app.action_next_user_message()
        release.set()
        async with asyncio.timeout(10):
            while app._history_loading:
                await pilot.pause()
        await pilot.pause()
        assert len(history.entries) == 400
        if target == "bottom":
            assert conversation.held_scroll_y() is None
            assert conversation.scroll_y == conversation.max_scroll_y
        else:
            assert conversation.scroll_y == history.user_lines[209 if target == "previous" else 211]


async def test_upward_scroll_retries_a_failed_page_without_automatic_retry(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 405)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    calls = 0
    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        conversation = app.query_one(ConversationView)
        await history.wait_until_rendered()
        await pilot.pause()
        original = app._controller.history_window

        async def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("Temporary page read failure")
            return await original(*args, **kwargs)

        monkeypatch.setattr(app._controller, "history_window", fail_once)
        conversation.scroll_to(y=1, animate=False)
        await pilot.pause()
        conversation.action_scroll_up()
        async with asyncio.timeout(10):
            while not app._history_load_failed or app._history_loading:
                await pilot.pause()
        await pilot.pause()
        assert calls == 1
        assert len(history.entries) == 200
        conversation.post_message(
            events.MouseScrollUp(conversation, 0, 0, 0, -1, 0, False, False, False)
        )
        async with asyncio.timeout(10):
            while len(history.entries) != 400 or app._history_loading:
                await pilot.pause()
        await pilot.pause()
        assert calls == 2
        assert not app._history_load_failed
        assert conversation.scroll_y == history.user_lines[200]


async def test_failed_selection_releases_inflight_history_request(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 605)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    started, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app) as pilot:
        original = app._controller.history_window

        async def held(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app._controller, "history_window", held)
        app._request_earlier_history()
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(SessionNotFoundError):
            await app._open_session("missing-session")
        release.set()
        await app.workers.wait_for_complete(
            [worker for worker in app.workers if worker.name == "_load_earlier_history"]
        )
        assert not app._history_loading
        app._request_earlier_history()
        async with asyncio.timeout(15):
            while app._history_loading:
                await pilot.pause()
        assert len(app.query_one(ConversationHistory).entries) == 400


async def test_relative_user_navigation_uses_history_coordinates(tmp_path):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 405)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        await history.wait_until_rendered()
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        conversation.release_anchor()
        conversation.scroll_to_user_turn(10)
        await pilot.pause()
        position = conversation.scroll_y
        conversation.scroll_to_previous_user_turn()
        await pilot.pause()
        assert conversation.scroll_y == position - (history.user_lines[10] - history.user_lines[9])
        conversation.scroll_to_next_user_turn()
        await pilot.pause()
        assert conversation.scroll_y == position


async def test_page_keys_move_by_one_viewport_without_animation(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 30)
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        conversation = app.query_one(ConversationView)
        await history.wait_until_rendered()
        conversation.focus()
        conversation.scroll_to(y=100, animate=False)
        await pilot.pause()
        start = conversation.scroll_y
        page_height = conversation.scrollable_content_region.height

        def forbid_animation(*args, **kwargs):
            pytest.fail("Page navigation started a scrolling animation")

        monkeypatch.setattr(conversation, "animate", forbid_animation)
        await pilot.press("pageup")
        assert conversation.scroll_y == start - page_height
        await pilot.press("pagedown")
        assert conversation.scroll_y == start


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_window_reset_keeps_loaded_prefix_only_within_the_same_epoch(tmp_path, backend):
    application = streaming_application(tmp_path, None, backend=backend)
    session, messages = seed(application, 405)
    store = application.runtime.store
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        assert await app._prepend_history_window(app._view_revision)
        await app.query_one(ConversationHistory).wait_until_rendered()
        store.save_session(
            store.load_session_record(session.session_id).model_copy(update={"title": "resumed"})
        )
        revision = store.observation_revision(session.session_id)
        async with asyncio.timeout(10):
            while app._observation_subscription.applied_position.revision < revision:
                await pilot.pause()
        assert len(app.query_one(ConversationHistory).entries) == 400
        withdrawal = store.commit_session(
            SessionCommit(
                before=store.load_session_record(session.session_id),
                before_message_id=messages[3].message_id,
                events=(
                    EventRecord(
                        session_id=session.session_id, event_type="session_history_withdrawn"
                    ),
                ),
            )
        )
        async with asyncio.timeout(10):
            while app._projection.history_epoch != withdrawal.session.history_epoch:
                await pilot.pause()
        await app.query_one(ConversationHistory).wait_until_rendered()
        assert [entry.text for entry in app.query_one(ConversationHistory).entries] == [
            message.text_content for message in messages[:3]
        ]
        assert app._history_before_cursor is None


async def test_history_reset_stops_the_previous_navigation_request(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, None)
    session, messages = seed(application, 605)
    store = application.runtime.store
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app) as pilot:
        history = app.query_one(ConversationHistory)
        wait_for_render = history.wait_until_rendered

        async def held_wait():
            await wait_for_render()
            if not entered.is_set():
                entered.set()
                await release.wait()

        monkeypatch.setattr(history, "wait_until_rendered", held_wait)
        app._request_earlier_history(all_pages=True, navigate_to="top")
        try:
            await asyncio.wait_for(entered.wait(), 5)
            withdrawal = store.commit_session(
                SessionCommit(
                    before=store.load_session_record(session.session_id),
                    before_message_id=messages[404].message_id,
                    events=(
                        EventRecord(
                            session_id=session.session_id, event_type="session_history_withdrawn"
                        ),
                    ),
                )
            )
            await wait_until(
                lambda: app._projection.history_epoch == withdrawal.session.history_epoch,
                pilot,
                description="replacement history snapshot",
            )
        finally:
            release.set()
        await app.workers.wait_for_complete(
            [worker for worker in app.workers if worker.group == "earlier-history"]
        )
        await wait_for_render()
        assert len(history.entries) == 200
        assert history.entries[-1].text == "message 403"
        assert app._history_before_cursor is not None


@pytest.mark.parametrize("run_message_count", [5, 300])
async def test_older_pages_present_each_run_summary_at_its_last_message(
    tmp_path, run_message_count
):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 0)
    store = application.runtime.store
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        started_at=utc_now() - timedelta(seconds=2),
        ended_at=utc_now(),
    )
    record = store.load_session_record(session.session_id)
    store.commit_session(
        SessionCommit(
            before=record, after=record.model_copy(update={"last_run_id": run.run_id}), runs=(run,)
        )
    )
    store.append_messages(
        [
            MessageRecord.text(
                session_id=session.session_id,
                run_id=run.run_id if index < run_message_count else None,
                role="user",
                text=f"message {index}",
            )
            for index in range(405)
        ]
    )
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app) as pilot:
        app._request_earlier_history(all_pages=True)
        async with asyncio.timeout(15):
            while app._history_loading:
                await pilot.pause()
        entries = app.query_one(ConversationHistory).entries
        assert sum(entry.kind == "run" for entry in entries) == 1
        assert entries[run_message_count].kind == "run"


async def test_older_page_finishing_after_a_switch_cannot_mutate_the_new_view(
    tmp_path, monkeypatch
):
    application = streaming_application(tmp_path, None)
    session, _messages = seed(application, 405)
    other = application.runtime.sessions.create(title="Other").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app):
        original = app._controller.history_window

        async def held(*args, **kwargs):
            page = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return page

        monkeypatch.setattr(app._controller, "history_window", held)
        loading = asyncio.create_task(app._prepend_history_window(app._view_revision))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await app._open_session(other.session_id)
        finally:
            release.set()
        assert not await loading
        assert app._projection.session_id == other.session_id
        assert not app.query_one(ConversationHistory).entries
        assert app._history_before_cursor is None


async def test_cached_reentry_retains_run_boundaries_in_hidden_scaffold_messages(tmp_path):
    application = streaming_application(tmp_path, None)
    session, _ = seed(application, 0)
    store = application.runtime.store
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        started_at=utc_now() - timedelta(seconds=2),
        ended_at=utc_now(),
    )
    record = store.load_session_record(session.session_id)
    store.commit_session(SessionCommit(before=record, runs=(run,)))
    store.append_messages(
        [
            MessageRecord.text(
                session_id=session.session_id,
                role="user",
                text=f"message {index}",
                run_id=run.run_id if index < 300 else None,
                scaffold=200 <= index < 300,
            )
            for index in range(405)
        ]
    )
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        await app._open_session(session.session_id)
        history = app.query_one(ConversationHistory)
        await history.wait_until_rendered()
        assert not any(entry.run_id == run.run_id for entry in history.entries)
        assert run.run_id in app._history_run_ids
        await app._new_session()
        await pilot.pause()
        store.append_messages(
            [
                MessageRecord.text(session_id=session.session_id, role="user", text=f"new {index}")
                for index in range(195)
            ]
        )
        await app._open_session(session.session_id)
        await history.wait_until_rendered()
        assert run.run_id in app._history_run_ids
        assert await app._prepend_history_window(app._view_revision)
        await history.wait_until_rendered()
        assert not any(entry.kind == "run" for entry in history.entries)
