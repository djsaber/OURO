from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.application.contracts.execution import ShutdownPolicy
from ouro.application.inputs.requests import UserInputRequest
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.messages import MessageRecord
from ouro.core.provider import ProviderStreamEvent
from tests.tui.test_tasks import streaming_application


class HeldProvider:
    provider_id = "faux"

    def __init__(self):
        self.started = asyncio.Queue()
        self.release = asyncio.Event()
        self.active = 0
        self.maximum = 0
        self.closed = asyncio.Event()

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.started.put_nowait(messages[-1].text_content)
        try:
            await self.release.wait()
            yield ProviderStreamEvent(type="text_delta", text="finished")
            yield ProviderStreamEvent(type="done")
        finally:
            self.active -= 1
            self.closed.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_acceptance_and_retries_do_not_wait_for_execution_capacity(tmp_path, backend):
    provider = HeldProvider()
    app = streaming_application(tmp_path, provider, backend=backend)
    app.supervisor.max_concurrency = 1
    supervisor = app.supervisor
    try:
        first = await supervisor.submit_user_input(UserInputRequest(prompt="first"))
        assert await asyncio.wait_for(provider.started.get(), 5) == "first"
        request = UserInputRequest(prompt="second")
        second = await supervisor.submit_user_input(request)
        retry = await supervisor.submit_user_input(request)
        assert retry.receipt == second.receipt
        assert provider.active == 1
        queued = app.runtime.queued_inputs.eligible_session(
            app.runtime.store.load_session_record(second.receipt.session_id)
        )
        assert len(queued) == 1
        assert queued[0].source == "user"
        assert queued[0].role == "user"
        assert not queued[0].scaffold
        ready = app.runtime.pending_user_inputs(second.receipt.session_id)
        assert len(ready) == 1
        assert ready[0].waiting_on_run_id is None
        ready_snapshot = app.runtime.observation_snapshot(second.receipt.session_id)
        assert ready_snapshot.queued_inputs == ready
        assert not ready_snapshot.messages
        append = await supervisor.submit_user_input(
            UserInputRequest(prompt="follow up", session_id=first.receipt.session_id)
        )
        running = await append.wait()
        assert running.run.status == "running"
        waiting = app.runtime.pending_user_inputs(first.receipt.session_id)
        assert len(waiting) == 1
        assert waiting[0].waiting_on_run_id == running.run.run_id
        waiting_snapshot = app.runtime.observation_snapshot(first.receipt.session_id)
        assert waiting_snapshot.queued_inputs == waiting
        assert waiting_snapshot.session.active_run_id == waiting[0].waiting_on_run_id
        assert provider.active == 1
        with pytest.raises(ValueError, match="already bound"):
            await supervisor.submit_user_input(request.model_copy(update={"prompt": "changed"}))
        provider.release.set()
        completed = await asyncio.wait_for(second.wait(), 10)
        assert completed.run.status == "completed"
        consumed = app.runtime.observation_snapshot(second.receipt.session_id)
        assert not consumed.queued_inputs
        assert consumed.messages[0].text_content == "second"
        after = await supervisor.submit_user_input(request)
        assert after.receipt.run_id == completed.run.run_id
        assert (await after.wait()).run.run_id == completed.run.run_id
        await asyncio.wait_for(first.wait(), 10)
        messages = app.runtime.store.load_session(second.receipt.session_id).messages
        assert [message.text_content for message in messages if message.role == "user"] == [
            "second"
        ]
        assert provider.maximum == 1
    finally:
        provider.release.set()
        await app.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_host_cancel_during_admission_still_schedules_and_can_retry(
    tmp_path, monkeypatch, backend
):
    provider = HeldProvider()
    app = streaming_application(tmp_path, provider, backend=backend)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    accept = app.runtime._accept_initial_input

    def held_accept(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return accept(*args)

    monkeypatch.setattr(app.runtime, "_accept_initial_input", held_accept)
    request = UserInputRequest(prompt="owned admission")
    host = asyncio.create_task(app.supervisor.submit_user_input(request))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        host.cancel()
        with pytest.raises(asyncio.CancelledError):
            await host
        release.set()
        assert await asyncio.wait_for(provider.started.get(), 5) == request.prompt
        retry = await app.supervisor.submit_user_input(request)
        assert retry.receipt.queue_id == request.input_id
        result_wait = asyncio.create_task(retry.wait())
        result_wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await result_wait
        assert provider.active == 1
        job = app.supervisor._jobs_by_session[retry.receipt.session_id]
        snapshot = await app.supervisor.abort(retry.receipt.session_id)
        assert snapshot.status == "aborted"
        await asyncio.wait_for(provider.closed.wait(), 5)
        await asyncio.wait_for(job.done.wait(), 5)
        assert not app.supervisor._jobs_by_session
    finally:
        release.set()
        provider.release.set()
        await app.close()


async def test_shutdown_finishes_handed_off_admission_before_applying_policy(tmp_path, monkeypatch):
    provider = HeldProvider()
    app = streaming_application(tmp_path, provider)
    entered, release = asyncio.Event(), asyncio.Event()
    accept = app.runtime.accept_user_input

    async def held_accept(request):
        entered.set()
        await release.wait()
        return await accept(request)

    monkeypatch.setattr(app.runtime, "accept_user_input", held_accept)
    host = asyncio.create_task(
        app.supervisor.submit_user_input(UserInputRequest(prompt="shutdown"))
    )
    await asyncio.wait_for(entered.wait(), 5)
    shutdown = asyncio.create_task(app.supervisor.shutdown(ShutdownPolicy(mode="abort")))
    release.set()
    submission = await asyncio.wait_for(host, 5)
    await asyncio.wait_for(shutdown, 10)
    assert app.runtime.store.load_session_record(submission.receipt.session_id)
    assert not app.supervisor._input_admissions
    assert not app.supervisor._jobs_by_session
    assert provider.active == 0
    await app.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_two_supervisors_accept_one_input_identity(tmp_path, backend):
    provider = HeldProvider()
    app = streaming_application(tmp_path, provider, backend=backend)
    other = streaming_application(tmp_path, provider, backend=backend)
    assert isinstance(other.supervisor, RuntimeSupervisor)
    request = UserInputRequest(prompt="one input")
    try:
        first, second = await asyncio.gather(
            app.supervisor.submit_user_input(request), other.supervisor.submit_user_input(request)
        )
        assert first.receipt.queue_id == second.receipt.queue_id
        await asyncio.wait_for(provider.started.get(), 5)
        provider.release.set()
        await asyncio.gather(first.wait(), second.wait())
        messages = app.runtime.store.load_session(first.receipt.session_id).messages
        assert len([m for m in messages if m.role == "user"]) == 1
        assert provider.maximum == 1
    finally:
        provider.release.set()
        await asyncio.gather(app.close(), other.close())


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_cancel_queued_submission_does_not_restart_on_retry(tmp_path, backend):
    provider = HeldProvider()
    app = streaming_application(tmp_path, provider, backend=backend)
    app.supervisor.max_concurrency = 1
    try:
        first = await app.supervisor.submit_user_input(UserInputRequest(prompt="first"))
        await asyncio.wait_for(provider.started.get(), 5)
        request = UserInputRequest(prompt="cancel before execution")
        queued = await app.supervisor.submit_user_input(request)
        status = await app.supervisor.abort(queued.receipt.session_id)
        assert status.status == "aborted"
        assert not app.runtime.pending_user_inputs(queued.receipt.session_id)
        with pytest.raises(asyncio.CancelledError):
            await queued.wait()
        with pytest.raises(ValueError, match="withdrawn or cancelled"):
            await app.supervisor.submit_user_input(request)
        provider.release.set()
        await asyncio.wait_for(first.wait(), 5)
        assert provider.started.empty()
    finally:
        provider.release.set()
        await app.close()


async def test_cancel_before_handoff_does_not_accept_input(tmp_path):
    app = streaming_application(tmp_path, HeldProvider())
    request = UserInputRequest(prompt="not handed off")
    host = asyncio.create_task(app.supervisor.submit_user_input(request))
    host.cancel()
    with pytest.raises(asyncio.CancelledError):
        await host
    with pytest.raises(FileNotFoundError):
        app.runtime.store.load_session_record(request.new_session_id)
    assert not app.supervisor._input_admissions
    await app.close()


async def test_execution_failure_releases_private_job_and_reports_original_error(
    tmp_path, monkeypatch
):
    app = streaming_application(tmp_path, HeldProvider())

    async def fail(*args, **kwargs):
        raise OSError("execution preparation failed")

    monkeypatch.setattr(app.runtime, "consume_host_input", fail)
    try:
        submission = await app.supervisor.submit_user_input(UserInputRequest(prompt="failed job"))
        with pytest.raises(OSError, match="execution preparation failed"):
            await submission.wait()
        assert app.runtime.pending_user_inputs(submission.receipt.session_id)
        assert not app.supervisor._jobs_by_session
        assert not app.supervisor._completion_events
        assert not app.supervisor._snapshots
    finally:
        await app.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_pending_input_receipt_does_not_search_canonical_message_history(
    tmp_path, monkeypatch, backend
):
    app = streaming_application(tmp_path, HeldProvider(), backend=backend)
    session = app.runtime.sessions.create().session
    store = app.runtime.store
    store.append_messages(
        [
            MessageRecord.text(session_id=session.session_id, role="user", text=f"history {i}")
            for i in range(200)
        ]
    )
    lookups = []
    find = store.find_queued_input_message

    def counted_find(*args):
        lookups.append(args)
        return find(*args)

    monkeypatch.setattr(store, "find_queued_input_message", counted_find)
    try:
        request = UserInputRequest(session_id=session.session_id, prompt="still pending")
        first = await app.runtime.accept_user_input(request)
        retry = await app.runtime.accept_user_input(request)
        assert first == retry
        assert first.message_id is None and first.run_id is None
        assert not lookups
        assert len(app.runtime.pending_user_inputs(session.session_id)) == 1
    finally:
        await app.close()
