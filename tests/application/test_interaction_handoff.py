import asyncio

import pytest

from ouro.core.callbacks import run_blocking
from tests.tui.app.support import _multi_approval_application, _question_application


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_immediate_interaction_waits_for_announcing_run_to_release(
    tmp_path, monkeypatch, kind, cancel_owner
):
    application = (
        _multi_approval_application(tmp_path, (tmp_path / "first.txt", tmp_path / "second.txt"))
        if kind == "approval"
        else _question_application(tmp_path, tool_name="PluginQuestion")
    )
    await application.start()
    runtime = application.runtime
    entered, release = asyncio.Event(), asyncio.Event()
    release_signal = runtime.run_ownership.release_signal

    async def paused_release(run_id, signal):
        if not entered.is_set():
            entered.set()
            await release.wait()
        await release_signal(run_id, signal)

    monkeypatch.setattr(runtime.run_ownership, "release_signal", paused_release)
    running = asyncio.create_task(
        runtime.run("ask", approval_mode="approval" if kind == "approval" else "inherit")
    )
    response = None
    contending = asyncio.Event()
    acquire = runtime.run_ownership.acquire

    async def observed_acquire(*args, **kwargs):
        lock = await acquire(*args, **kwargs)
        if asyncio.current_task() is response and lock is None:
            contending.set()
        return lock

    monkeypatch.setattr(runtime.run_ownership, "acquire", observed_acquire)
    try:
        await asyncio.wait_for(entered.wait(), 60)
        session = (await run_blocking(runtime.store.list_sessions))[0]
        snapshot = await run_blocking(runtime.store.load_runtime_session, session.session_id)
        run = snapshot.runs[-1]
        response = asyncio.create_task(
            runtime.approve_tool(session_id=session.session_id, run_id=run.run_id, approved=True)
            if kind == "approval"
            else runtime.answer_user(
                session_id=session.session_id, run_id=run.run_id, answer="Safely"
            )
        )
        await asyncio.wait_for(contending.wait(), 60)
        assert not response.done()
        if cancel_owner:
            running.cancel()
        release.set()
        initial, resumed = await asyncio.wait_for(
            asyncio.gather(running, response, return_exceptions=True), 60
        )
        if cancel_owner:
            assert isinstance(initial, asyncio.CancelledError)
        else:
            assert initial.run.status == (
                "waiting_approval" if kind == "approval" else "waiting_user"
            )
        assert resumed.run.status == ("waiting_approval" if kind == "approval" else "completed")
        if kind == "approval":
            assert resumed.run.pending_approval_id != run.pending_approval_id
    finally:
        release.set()
        await asyncio.gather(
            running, *([response] if response is not None else []), return_exceptions=True
        )
        await application.close()
