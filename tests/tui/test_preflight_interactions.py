from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.tui.controller import TuiController
from ouro.tui.state import TuiSessionProjection
from tests.tui.app.support import _approval_application


@pytest.mark.parametrize("approved", [False, True])
async def test_preflight_decision_waits_for_its_durable_checkpoint(tmp_path, monkeypatch, approved):
    target = tmp_path / "approved.txt"
    application = _approval_application(tmp_path, target)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
        approval_mode="approval",
    )
    reached, release = threading.Event(), threading.Event()
    subscribed = asyncio.Event()
    save_batch = application.runtime.run_lifecycle.save_batch_state
    subscribe = application.supervisor.subscribe

    def gated_checkpoint(**kwargs):
        if kwargs["status"] == "waiting_approval" and not release.is_set():
            reached.set()
            assert release.wait(15), "checkpoint was not released"
        return save_batch(**kwargs)

    async def observed_subscription(*args, **kwargs):
        subscription = await subscribe(*args, **kwargs)
        subscribed.set()
        return subscription

    monkeypatch.setattr(application.runtime.run_lifecycle, "save_batch_state", gated_checkpoint)
    await controller.start()
    submission = asyncio.create_task(controller.submit("request a tool"))
    decision = subscription_ready = None
    try:
        assert await asyncio.to_thread(reached.wait, 15), "preflight did not reach its checkpoint"
        session_id = controller.session_id
        assert session_id is not None
        snapshot = await asyncio.to_thread(application.runtime.store.load_session, session_id)
        projection = TuiSessionProjection(session_id)
        projection.restore(tuple(snapshot.events))
        interaction = projection.pending_interaction
        assert interaction is not None and interaction.kind == "approval"
        assert (
            application.runtime.store.load_run(session_id, interaction.run_id).status == "running"
        )

        monkeypatch.setattr(application.supervisor, "subscribe", observed_subscription)
        decision = asyncio.create_task(
            controller.resolve_approval(interaction=interaction, approved=approved)
        )
        subscription_ready = asyncio.create_task(subscribed.wait())
        done, _pending = await asyncio.wait(
            (decision, subscription_ready), timeout=15, return_when=asyncio.FIRST_COMPLETED
        )
        if decision in done:
            await decision
            pytest.fail("decision completed before its checkpoint was saved")
        assert subscription_ready in done, "decision did not observe the durable run"
        assert not target.exists()
        release.set()

        result = await asyncio.wait_for(decision, 15)
        assert result is not None and result.run.status == "completed"
        if approved:
            assert target.read_text(encoding="utf-8") == "written"
        else:
            assert not target.exists()
        saved = await asyncio.to_thread(application.runtime.store.load_session, session_id)
        resolved = [event for event in saved.events if event.event_type == "approval_resolved"]
        assert len(resolved) == 1
        assert resolved[0].payload["approved"] is approved
    finally:
        release.set()
        if subscription_ready is not None:
            subscription_ready.cancel()
            await asyncio.gather(subscription_ready, return_exceptions=True)
        tasks = (submission, decision) if decision is not None else (submission,)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
        finally:
            await controller.close()
