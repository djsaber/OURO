from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ouro.application.queued_inputs import QueuedUserInputView
from ouro.core.provider import ProviderStreamEvent
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation_history import ConversationHistory
from ouro.tui.screens import ChoiceScreen
from ouro.tui.widgets import Composer, ConversationView, InteractionBar, QueuedInputPanel
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import (
    _application,
    _approval_application,
    _persist_direct_child_session,
    _wait_for_screen,
)
from tests.tui.test_live_session_attachment import visible
from tests.tui.test_tasks import choose, streaming_application


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


async def submit(app, pilot, provider, prompt):
    previous_workers = set(app.workers)
    app.query_one(Composer).load_text(prompt)
    await pilot.press("enter")
    assert await asyncio.wait_for(provider.started.get(), 5) == prompt
    await visible(pilot, app, "prefix.")
    return app._controller.session_id, next(
        w for w in app.workers if w.group == "run" and w not in previous_workers
    )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("destination", ["welcome", "session"])
async def test_background_completion_preserves_destination_and_draft(
    tmp_path, backend, destination
):
    provider = NavigationProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    other = application.runtime.sessions.create(title="Other session").session
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
            await pilot.press("ctrl+s")
            await _wait_for_screen(app, ChoiceScreen, title="SESSION • USER")
            await choose(pilot, app, "Other session")
            expected_id = other.session_id
        assert subscription.closed
        app.query_one(Composer).load_text("Destination draft")
        await pilot.pause()
        projection = app._projection
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
        await app._open_session(session_id)
        await visible(pilot, app, "Finished")
        assert app.query_one(Composer).text == "Alpha draft"
        assert app._projection.activity.status == "completed"
        entries = app.query_one(ConversationHistory).entries
        assert sum(entry.kind == "run" for entry in entries) == 1


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
    app = OuroTuiApp(application=streaming_application(tmp_path, NavigationProvider()))
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*args, **kwargs):
        entered.set()
        await release.wait()
        raise ValueError("Submission failed")

    async with run_ready_app(app, size=(100, 32)) as pilot:
        monkeypatch.setattr(app._controller, "submit", fail)
        app._submit_run("Original prompt", "follow_up")
        await asyncio.wait_for(entered.wait(), 5)
        worker = next(w for w in app.workers if w.group == "run")
        await app._new_session()
        release.set()
        await worker.wait()
        await pilot.pause()
        assert app.query_one(ConversationView).showing_welcome
        assert app.query_one(Composer).text == ""
        assert app._input_drafts.draft_for(None) == ""


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
            await pilot.press("ctrl+s")
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


async def test_queue_read_finishing_after_navigation_does_not_replace_destination_queue(
    tmp_path, monkeypatch
):
    application = _application(tmp_path)
    source = application.runtime.sessions.create(title="Source").session
    app = OuroTuiApp(application=application, resume_session_id=source.session_id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_inputs(session_id):
        assert session_id == source.session_id
        entered.set()
        await release.wait()
        return (
            QueuedUserInputView(
                queue_id="background-input",
                prompt="Background follow-up",
                mode="follow_up",
                queued_at=datetime.now(UTC),
            ),
        )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        monkeypatch.setattr(app._controller, "queued_user_inputs", delayed_inputs)
        refresh = asyncio.create_task(app._sync_queued_inputs())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await app._new_session()
        finally:
            release.set()
        await refresh
        await pilot.pause()
        assert not app.query_one(QueuedInputPanel).display
        assert app.query_one(ConversationView).showing_welcome
