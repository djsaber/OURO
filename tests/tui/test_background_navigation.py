from __future__ import annotations

import asyncio
import gc
import warnings
from datetime import UTC, datetime

import pytest

from ouro.application.queued_inputs import QueuedUserInputView
from ouro.application.supervisor_models import ShutdownPolicy
from ouro.core.billing import UsageFilter
from ouro.core.messages import MessageRecord
from ouro.core.provider import ProviderStreamEvent
from ouro.core.usage_queries import UsageSummary, UsageTotals
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation_history import ConversationHistory
from ouro.tui.screens import ChoiceScreen
from ouro.tui.widgets import (
    ActivityIndicator,
    CommandPopup,
    Composer,
    ConversationView,
    InteractionBar,
    QueuedInputPanel,
    WelcomePanel,
)
from tests.tui.helpers import run_ready_app, wait_until
from tests.tui.test_app import (
    _application,
    _approval_application,
    _persist_direct_child_session,
    _wait_for_screen,
)
from tests.tui.test_live_session_attachment import visible
from tests.tui.test_tasks import choose, streaming_application


async def test_agent_tree_cancelled_before_start_closes_without_coroutine_warning(
    tmp_path, monkeypatch
):
    application = _application(tmp_path)
    session = application.runtime.sessions.create().session
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        run_worker = app.run_worker

        def cancel_tree_immediately(work, **kwargs):
            worker = run_worker(work, **kwargs)
            if kwargs.get("group") == "agent-tree":
                worker.cancel()
            return worker

        monkeypatch.setattr(app, "run_worker", cancel_tree_immediately)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            await app._replace_agent_tree(session.session_id)
            subscription = app._agent_tree_subscription
            await app._replace_agent_tree(None)
            await pilot.pause()
            gc.collect()
        assert subscription is not None
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(subscription.get(), timeout=1)
        assert not [warning for warning in caught if "_consume_agent_tree" in str(warning.message)]


class NavigationProvider:
    provider_id = "faux"

    def __init__(self):
        self.streams = {}
        self.started = asyncio.Queue()

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
        prompt = messages[-1].text_content
        queue = self.streams[prompt] = asyncio.Queue()
        self.started.put_nowait(prompt)
        yield ProviderStreamEvent(type="text_delta", text=f"{prompt} prefix. ")
        while True:
            event = await queue.get()
            yield event
            if event.type == "done":
                return

    def text(self, prompt, text):
        self.streams[prompt].put_nowait(ProviderStreamEvent(type="text_delta", text=text))

    def finish(self, prompt):
        self.streams[prompt].put_nowait(ProviderStreamEvent(type="done"))


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("destination", ["home", "empty", "completed"])
async def test_idle_submission_does_not_present_follow_up_queue(
    tmp_path, monkeypatch, backend, destination
):
    provider = NavigationProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    previous_turns = ()
    if destination == "completed":
        previous = asyncio.create_task(application.runtime.run("Previous input"))
        assert await asyncio.wait_for(provider.started.get(), 5) == "Previous input"
        provider.finish("Previous input")
        session_id = (await previous).session.session_id
        previous_turns = ("Previous input",)
    elif destination == "empty":
        session_id = application.runtime.sessions.create().session.session_id
    app = OuroTuiApp(application=application)
    entered, release = asyncio.Event(), asyncio.Event()
    original = application.supervisor._dispatch_host_input

    async def held_dispatch(session_id):
        entered.set()
        await release.wait()
        return await original(session_id)

    monkeypatch.setattr(application.supervisor, "_dispatch_host_input", held_dispatch)
    async with run_ready_app(app) as pilot:
        if destination != "home":
            await app._open_session(session_id)
            await pilot.pause()
        welcome = app.query_one(WelcomePanel)
        composer = app.query_one(Composer)
        queue_panel = app.query_one(QueuedInputPanel)
        transitions = []
        set_mode = welcome.set_welcome_mode

        def record_mode(enabled):
            transitions.append(enabled)
            set_mode(enabled)

        submitting, allow_submit = asyncio.Event(), asyncio.Event()
        submit = app._controller.submit
        submissions = 0

        async def held_submit(*args, **kwargs):
            nonlocal submissions
            submissions += 1
            submitting.set()
            await allow_submit.wait()
            return await submit(*args, **kwargs)

        monkeypatch.setattr(welcome, "set_welcome_mode", record_mode)
        monkeypatch.setattr(app._controller, "submit", held_submit)
        composer.load_text("First input")
        await pilot.press("enter")
        try:
            await asyncio.wait_for(submitting.wait(), 5)
            await pilot.pause()
            assert composer.has_focus and not composer.disabled
            await pilot.press(*"Next draft")
            if destination == "home":
                await pilot.press("enter")
            assert composer.text == "Next draft"
            assert submissions == 1
            allow_submit.set()
            await asyncio.wait_for(entered.wait(), 5)
            await wait_until(
                lambda: app._projection is not None and queue_panel.inputs,
                pilot,
                description="accepted input observation before execution",
            )
            session_id = app._controller.session_id
            assert app.query_one(ConversationHistory).user_turns == previous_turns
            assert not queue_panel.display
            assert "Queued" in app.query_one(ActivityIndicator).render().plain
            assert not welcome.welcome_active
            assert not app.query_one(ConversationView).showing_welcome
            assert True not in transitions
            accepted_snapshot = application.runtime.observation_snapshot(session_id)

            # Explicit reentry also uses the accepted input, not only the empty body.
            await app._open_session(session_id)
            await pilot.pause()
            assert not welcome.welcome_active
            assert True not in transitions
            release.set()
            assert await asyncio.wait_for(provider.started.get(), 5) == "First input"
            await wait_until(
                lambda: (
                    app.query_one(ConversationHistory).user_turns
                    == (*previous_turns, "First input")
                    and app._projection is not None
                    and "First input prefix." in app._projection.assistant_text
                ),
                pilot,
                description="new input and its own answer observed",
            )
            # Loading an older boundary must not mix in a newly consumed queue.
            accepted_view = await app._controller.load_observation_snapshot(accepted_snapshot)
            assert [item.prompt for item in accepted_view.queued_inputs] == ["First input"]
            assert accepted_view.detail.session.active_run_id is None
            assert True not in transitions
            assert composer.text == "Next draft"
            assert composer.has_focus and not composer.disabled
            assert not queue_panel.display
            assert not app.query_one(CommandPopup).display
            provider.finish("First input")
            await app._new_session()
            assert welcome.welcome_active
            assert app.query_one(ConversationView).showing_welcome
        finally:
            allow_submit.set()
            release.set()
            await application.supervisor.shutdown(ShutdownPolicy(mode="abort"))


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("finish", ["complete", "abort"])
async def test_tui_submission_uses_capacity_and_host_detach_preserves_execution(
    tmp_path, backend, finish
):
    provider = NavigationProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    application.supervisor.max_concurrency = 1
    app = OuroTuiApp(application=application, close_application=False)
    try:
        async with run_ready_app(app, size=(100, 32)) as pilot:
            first_id, _ = await submit(app, pilot, provider, "Alpha")
            await app._new_session()
            composer = app.query_one(Composer)
            composer.load_text("Beta")
            composer.focus()
            await pilot.press("enter")
            await wait_until(
                lambda: len(application.supervisor._jobs_by_session) == 2,
                pilot,
                description="durable second submission waiting for capacity",
            )
            second_id = app._controller.session_id
            assert second_id is not None and second_id != first_id
            assert application.supervisor.status(second_id).status == "queued"
            await wait_until(
                lambda: "Queued" in app.query_one(ActivityIndicator).render().plain,
                pilot,
                description="accepted input waiting for capacity",
            )
            assert not app.query_one(QueuedInputPanel).display
            assert set(provider.streams) == {"Alpha"}
            provider.finish("Alpha")
            assert await asyncio.wait_for(provider.started.get(), 5) == "Beta"
            await visible(pilot, app, "prefix.")
            assert app._projection.session_id == second_id
            assert app._projection.assistant_text == "Beta prefix. "
            job = application.supervisor._jobs_by_session[second_id]
        assert not job.done.is_set()
        assert application.supervisor.status(second_id).status == "running"
        if finish == "abort":
            await application.supervisor.abort(second_id)
        else:
            provider.text("Beta", "after detach")
            provider.finish("Beta")
        await asyncio.wait_for(job.done.wait(), 5)
        snapshot = application.runtime.store.load_runtime_session(second_id)
        assert snapshot.runs[-1].status == ("aborted" if finish == "abort" else "completed")
        if finish == "complete":
            assert "after detach" in snapshot.messages[-1].text_content
        assert not application.supervisor._jobs_by_session
        assert not application.supervisor._completion_events
    finally:
        await application.close()


async def submit(app, pilot, provider, prompt):
    previous_workers = set(app.workers)
    composer = app.query_one(Composer)
    assert not composer.disabled
    composer.focus()
    composer.load_text(prompt)
    await pilot._wait_for_screen()
    await pilot.press("enter")
    await wait_until(
        lambda: not provider.started.empty(), pilot, description="submitted model stream"
    )
    assert provider.started.get_nowait() == prompt
    await visible(pilot, app, "prefix.")
    return app._controller.session_id, next(
        w for w in app.workers if w.group == "run" and w not in previous_workers
    )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("destination", ["welcome", "session"])
async def test_background_completion_preserves_destination_and_draft(
    tmp_path, backend, destination, monkeypatch
):
    provider = NavigationProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    other = application.runtime.sessions.create(title="Other session").session
    application.runtime.store.append_messages(
        [
            MessageRecord.text(
                session_id=other.session_id, sequence=1, role="user", text="Other history"
            )
        ]
    )
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        session_id, worker = await submit(app, pilot, provider, "Alpha")
        # Reopening first also exercises release of an actual live subscription.
        await app._open_session(session_id)
        subscription = app._observation_subscription
        app.query_one(Composer).load_text("Alpha draft")
        await pilot.pause()
        if destination == "welcome":
            await pilot.press("ctrl+n")
            async with asyncio.timeout(5):
                while not app.query_one(ConversationView).showing_welcome:
                    await pilot.pause()
            expected_id = None
        else:
            await pilot.press("ctrl+o")
            await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
            await choose(pilot, app, "Other session")
            expected_id = other.session_id
        async with asyncio.timeout(5):
            while app._controller.session_id != expected_id or not subscription.closed:
                await pilot.pause()
        assert subscription.closed
        app.query_one(Composer).load_text("Destination draft")
        await pilot.pause()
        projection = app._projection
        usage_queries = 0
        summary = app._controller.usage_summary

        async def count_usage():
            nonlocal usage_queries
            usage_queries += 1
            return await summary()

        monkeypatch.setattr(app._controller, "usage_summary", count_usage)
        provider.text("Alpha", "Finished in background.")
        provider.finish("Alpha")
        await asyncio.wait_for(worker.wait(), 8)
        await pilot.pause()

        assert app._controller.session_id == expected_id
        assert app._projection is projection
        assert app.query_one(Composer).text == "Destination draft"
        assert not app.query_one(Composer).disabled
        assert "Finished" not in app.export_screenshot()
        assert not app.query_one(QueuedInputPanel).display
        assert usage_queries == (1 if destination == "welcome" else 0)
        await app._open_session(session_id)
        await pilot.pause()
        await visible(pilot, app, "Finished")
        assert app.query_one(Composer).text == "Alpha draft"
        assert app._projection.activity.status == "completed"
        entries = app.query_one(ConversationHistory).entries
        assert sum(entry.kind == "run" for entry in entries) == 1


async def test_welcome_usage_coalesces_concurrent_completions_and_keeps_latest_totals(
    tmp_path, monkeypatch
):
    app = OuroTuiApp(application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    completed = 1

    async def summary():
        nonlocal calls
        calls += 1
        value = completed
        if calls == 1:
            entered.set()
            await release.wait()
        return UsageSummary(usage_filter=UsageFilter(), totals=UsageTotals(request_count=value))

    async with run_ready_app(app) as pilot:
        monkeypatch.setattr(app._controller, "usage_summary", summary)
        refresh = asyncio.create_task(app._refresh_welcome_status())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for count in range(2, 10):
                completed = count
                await app._refresh_welcome_status()
            assert calls == 1
        finally:
            release.set()
        await refresh
        await pilot.pause()
        assert calls == 2
        assert app._system_status.usage_request_count == 9
        assert app.query_one(WelcomePanel)._status.usage_request_count == 9


@pytest.mark.parametrize("reenter_before_read", [False, True])
async def test_usage_refresh_does_not_update_hidden_welcome_and_reentry_reads_current_totals(
    tmp_path, monkeypatch, reenter_before_read
):
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Selected").session
    application.runtime.store.append_messages(
        [MessageRecord.text(session_id=session.session_id, sequence=1, role="user", text="History")]
    )
    app = OuroTuiApp(application=application)
    entered, release = asyncio.Event(), asyncio.Event()
    full_status_read, publish_full_status = asyncio.Event(), asyncio.Event()
    calls = 0

    async def summary():
        nonlocal calls
        calls += 1
        value = calls
        if calls == 1:
            entered.set()
            await release.wait()
        return UsageSummary(usage_filter=UsageFilter(), totals=UsageTotals(request_count=value))

    async with run_ready_app(app) as pilot:
        system_status = app._controller.system_status

        async def held_system_status():
            status = await system_status()
            full_status_read.set()
            await publish_full_status.wait()
            return status

        monkeypatch.setattr(app._controller, "usage_summary", summary)
        monkeypatch.setattr(app._controller, "system_status", held_system_status)
        refresh = asyncio.create_task(app._refresh_welcome_status())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await app._open_session(session.session_id)
            status = app.query_one(WelcomePanel)._status
            await app._refresh_welcome_status()
            assert calls == 1
            if reenter_before_read:
                await app._new_session()
                # Reentry schedules a full refresh after layout. Make sure it
                # joins the in-flight read before releasing that stale result.
                await wait_until(
                    lambda: app._welcome_refresh_requested == "system",
                    pilot,
                    description="welcome reentry refresh queued behind the stale usage read",
                )
            release.set()
            if not reenter_before_read:
                await refresh
                assert app.query_one(WelcomePanel)._status is status
                await app._new_session()

            await asyncio.wait_for(full_status_read.wait(), 5)
            assert calls == 2
            # A completed usage query is not a published full snapshot. Neither
            # the hidden view nor the reentered view may receive the stale read.
            assert app.query_one(WelcomePanel)._status.usage_request_count == (
                status.usage_request_count
            )
        finally:
            release.set()
            publish_full_status.set()
            await refresh
        await wait_until(
            lambda: app.query_one(WelcomePanel)._status.usage_request_count == 2,
            pilot,
            description="current welcome usage snapshot published",
        )
        assert calls == 2
        assert app._system_status.usage_request_count == 2


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_two_owned_sessions_keep_streaming_across_welcome_and_reentry(tmp_path, backend):
    provider = NavigationProvider()
    app = OuroTuiApp(application=streaming_application(tmp_path, provider, backend=backend))
    async with run_ready_app(app, size=(100, 32)) as pilot:
        alpha, alpha_worker = await submit(app, pilot, provider, "Alpha")
        await app._dispatch_command("/new")
        beta, beta_worker = await submit(app, pilot, provider, "Beta")
        assert alpha != beta
        provider.text("Alpha", "While away. ")
        await app._dispatch_command("/sessions")
        await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
        await choose(pilot, app, "Alpha")
        await visible(pilot, app, "While")
        assert app._projection.activity.status == "running"
        provider.text("Alpha", "After return.")
        await visible(pilot, app, "After")
        assert not alpha_worker.is_finished

        provider.finish("Beta")
        await asyncio.wait_for(beta_worker.wait(), 8)
        assert app._controller.session_id == alpha
        assert app._projection.session_id == alpha
        assert app._projection.activity.status == "running"
        provider.finish("Alpha")
        await asyncio.wait_for(alpha_worker.wait(), 8)
        await pilot.pause()
        entries = app.query_one(ConversationHistory).entries
        answers = [entry.text for entry in entries if entry.kind == "assistant"]
        assert answers == ["Alpha prefix. While away. After return."]
        assert sum(entry.kind == "run" for entry in entries) == 1


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_three_running_sessions_keep_content_and_drafts_during_overlapping_switches(
    tmp_path, backend
):
    provider = NavigationProvider()
    app = OuroTuiApp(application=streaming_application(tmp_path, provider, backend=backend))
    sessions, workers = [], []
    async with run_ready_app(app, size=(100, 32)) as pilot:
        for prompt in ("Alpha", "Beta", "Gamma"):
            session_id, worker = await submit(app, pilot, provider, prompt)
            sessions.append(session_id)
            workers.append(worker)
            app.query_one(Composer).load_text(f"{prompt} draft")
            await pilot.pause()
            if prompt != "Gamma":
                await app._new_session()

        prior = app._observation_subscription
        switches = [
            asyncio.create_task(app._open_session(session_id))
            for session_id in (*sessions, sessions[0])
        ]
        provider.text("Alpha", "visible end")
        provider.text("Beta", "background end")
        provider.finish("Beta")
        provider.finish("Gamma")
        await asyncio.gather(*switches)
        await visible(pilot, app, "visible")
        assert prior.closed
        assert app._controller.session_id == sessions[0]
        assert app._projection.session_id == sessions[0]
        assert app.query_one(Composer).text == "Alpha draft"
        assert "background end" not in app.export_screenshot()
        assert not workers[0].is_finished
        provider.finish("Alpha")
        await asyncio.gather(*(worker.wait() for worker in workers))
        async with asyncio.timeout(8):
            while (
                app._projection.activity.status != "completed"
                or app._observation_subscription.applied_sequence < app._projection.last_sequence
            ):
                await pilot.pause()
        assert app.query_one(Composer).text == "Alpha draft"
        entries = app.query_one(ConversationHistory).entries
        assert [entry.text for entry in entries if entry.kind == "assistant"] == [
            "Alpha prefix. visible end"
        ]
        assert sum(entry.kind == "run" for entry in entries) == 1


async def test_waiting_approval_can_leave_and_restore_the_same_interaction(tmp_path):
    target = tmp_path / "approved.txt"
    app = OuroTuiApp(application=_approval_application(tmp_path, target))
    async with run_ready_app(app, size=(100, 32)) as pilot:
        app.query_one(Composer).load_text("Write the file")
        await pilot.press("enter")
        async with asyncio.timeout(8):
            while app._interaction is None:
                await pilot.pause()
        interaction = app._interaction
        session_id = app._controller.session_id
        assert app.query_one(InteractionBar).display
        await pilot.press("ctrl+n")
        async with asyncio.timeout(5):
            while not app.query_one(ConversationView).showing_welcome:
                await pilot.pause()
        assert not app.query_one(InteractionBar).display
        assert not app.query_one(Composer).disabled
        assert not target.exists()
        await app._open_session(session_id)
        assert app._interaction == interaction
        assert app.query_one(InteractionBar).display
        await app._resolve_approval(interaction, True).wait()
        assert target.exists()
        assert app._projection.activity.status == "completed"


async def test_late_submission_failure_does_not_restore_input_into_another_session(
    tmp_path, monkeypatch
):
    application = streaming_application(tmp_path, NavigationProvider())
    destination = application.runtime.create_session(title="Destination").session_id
    app = OuroTuiApp(application=application)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        raise ValueError("Submission failed")

    async with run_ready_app(app, size=(100, 32)) as pilot:
        app._remember_input_draft(destination, "Destination draft")
        monkeypatch.setattr(app._controller, "submit", fail)
        app._submit_run("Original prompt", "follow_up")
        await asyncio.wait_for(entered.wait(), 5)
        worker = next(w for w in app.workers if w.group == "run")
        await app._open_session(destination)
        release.set()
        await worker.wait()
        await pilot.pause()
        assert app._controller.session_id == destination
        assert app.query_one(ConversationView).showing_welcome
        assert app.query_one(Composer).text == "Destination draft"
        assert app._input_drafts.draft_for(destination) == "Destination draft"
        assert app._input_drafts.draft_for(None) == "Original prompt"


@pytest.mark.parametrize("existing_session", [False, True])
async def test_rejected_first_input_restores_welcome_and_draft(
    tmp_path, monkeypatch, existing_session
):
    application = _application(tmp_path)
    session_id = (
        application.runtime.create_session(title="Empty").session_id if existing_session else None
    )
    app = OuroTuiApp(application=application, resume_session_id=session_id)

    async def fail(*args, **kwargs):
        raise ValueError("Submission failed")

    async with run_ready_app(app) as pilot:
        monkeypatch.setattr(app._controller, "submit", fail)
        app.query_one(Composer).load_text("Unsent input")
        await pilot.press("enter")
        await wait_until(
            lambda: app.query_one(Composer).text == "Unsent input",
            pilot,
            description="rejected draft restored",
        )
        assert app.query_one(ConversationView).showing_welcome
        assert app.query_one(WelcomePanel).welcome_active
        assert not app.query_one(Composer).disabled


@pytest.mark.parametrize("destination", ["welcome", "session"])
async def test_leaving_child_restores_destination_draft_and_editable_surface(tmp_path, destination):
    application = _application(tmp_path)
    root, child = _persist_direct_child_session(application)
    other = application.runtime.sessions.create(title="Other session").session
    app = OuroTuiApp(application=application)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        app._remember_input_draft(None, "Welcome draft")
        app._remember_input_draft(other.session_id, "Other draft")
        await app._open_session(root)
        app.query_one(Composer).load_text("Root draft")
        await pilot.pause()
        await app._open_session(child)
        assert app.query_one(Composer).disabled
        if destination == "welcome":
            await pilot.press("ctrl+n")
            async with asyncio.timeout(5):
                while not app.query_one(ConversationView).showing_welcome:
                    await pilot.pause()
            expected_draft = "Welcome draft"
        else:
            await pilot.press("ctrl+o")
            await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
            await choose(pilot, app, "Other session")
            expected_draft = "Other draft"
        composer = app.query_one(Composer)
        assert not app._visible_read_only
        assert not composer.disabled
        assert not composer.has_class("read-only-session")
        assert composer.text == expected_draft
        await app._open_session(root)
        assert composer.text == "Root draft"


@pytest.mark.parametrize("destination", ("new", "reload", "snapshot"))
async def test_queue_read_finishing_after_navigation_does_not_replace_destination_queue(
    tmp_path, monkeypatch, destination
):
    application = _application(tmp_path)
    source = application.runtime.sessions.create(title="Source").session
    app = OuroTuiApp(application=application, resume_session_id=source.session_id)
    entered, release, requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reads = 0

    async def delayed_inputs(session_id):
        nonlocal reads
        assert session_id == source.session_id
        reads += 1
        entered.set()
        await release.wait()
        return (
            QueuedUserInputView(
                queue_id="background-input",
                prompt="Background follow-up",
                mode="follow_up",
                queued_at=datetime.now(UTC),
                waiting_on_run_id="run_source",
            ),
        )

    async def refresh_before_navigation():
        requested.set()
        await app._sync_queued_inputs()

    async with run_ready_app(app, size=(100, 32)) as pilot:
        monkeypatch.setattr(app._controller, "queued_user_inputs", delayed_inputs)
        refreshes = [asyncio.create_task(app._sync_queued_inputs())]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            refreshes.append(asyncio.create_task(refresh_before_navigation()))
            await asyncio.wait_for(requested.wait(), 5)
            if destination == "new":
                await app._new_session()
            elif destination == "reload":
                await app._open_session(source.session_id)
            else:
                observation = await app._controller.observe_session(source.session_id)
                try:
                    await app._install_session_surface(
                        observation.loaded,
                        subscription=observation.subscription,
                        revision=app._view_revision,
                    )
                finally:
                    if app._observation_subscription is not observation.subscription:
                        observation.subscription.close()
        finally:
            release.set()
            await asyncio.gather(*refreshes)
        await pilot.pause()
        assert reads == 1
        assert app.query_one(QueuedInputPanel).inputs == ()
        assert not app.query_one(QueuedInputPanel).display
        assert app.query_one(ConversationView).showing_welcome


@pytest.mark.parametrize("change", ("enqueue", "unqueue", "consume"))
async def test_queue_refresh_serializes_reads_and_preserves_latest_inputs(
    tmp_path, monkeypatch, change
):
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Queued inputs").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    first, second = (
        QueuedUserInputView(
            queue_id=f"queued_{index}",
            prompt=prompt,
            mode="follow_up",
            queued_at=datetime.now(UTC),
            waiting_on_run_id="run_active",
        )
        for index, prompt in enumerate(("keep queued", "return to composer"))
    )
    previous, current = {
        "enqueue": ((first,), (first, second)),
        "unqueue": ((first, second), (first,)),
        "consume": ((first, second), ()),
    }[change]
    entered, release, requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reads = 0

    async def delayed_inputs(session_id):
        nonlocal reads
        assert session_id == session.session_id
        reads += 1
        if reads == 1:
            entered.set()
            await release.wait()
            return previous
        return current

    async def refresh_after_change():
        requested.set()
        await app._sync_queued_inputs()

    async with run_ready_app(app) as pilot:
        monkeypatch.setattr(app._controller, "queued_user_inputs", delayed_inputs)
        refreshes = [asyncio.create_task(app._sync_queued_inputs())]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            refreshes.append(asyncio.create_task(refresh_after_change()))
            await asyncio.wait_for(requested.wait(), 5)
            # Snapshot freshness follows actual reads, not task creation order.
            assert reads == 1
        finally:
            release.set()
            await asyncio.gather(*refreshes)
        await pilot.pause()
        panel = app.query_one(QueuedInputPanel)
        assert panel.inputs == current
        assert panel.display is bool(current)
        assert tuple(option.value for option in app._queued_input_arguments()) == tuple(
            item.queue_id for item in current
        )


@pytest.mark.parametrize("outcome", ("error", "cancel"))
async def test_failed_queue_read_releases_the_next_refresh(tmp_path, monkeypatch, outcome):
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Queue recovery").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    entered, release, requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reads = 0

    async def delayed_inputs(session_id):
        nonlocal reads
        assert session_id == session.session_id
        reads += 1
        if reads == 1:
            entered.set()
            await release.wait()
            raise OSError("queue read failed")
        return ()

    async def refresh_after_failure():
        requested.set()
        await app._sync_queued_inputs()

    async with run_ready_app(app):
        monkeypatch.setattr(app._controller, "queued_user_inputs", delayed_inputs)
        refreshes = [asyncio.create_task(app._sync_queued_inputs())]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            refreshes.append(asyncio.create_task(refresh_after_failure()))
            await asyncio.wait_for(requested.wait(), 5)
            assert reads == 1
            if outcome == "cancel":
                refreshes[0].cancel()
            else:
                release.set()
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else OSError):
                await refreshes[0]
            await asyncio.wait_for(asyncio.shield(refreshes[1]), 5)
        finally:
            release.set()
            await asyncio.gather(*refreshes, return_exceptions=True)
        assert reads == 2
        assert app.query_one(QueuedInputPanel).inputs == ()
