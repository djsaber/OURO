from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.application.contracts.execution import ShutdownPolicy
from ouro.application.inputs.requests import UserInputRequest
from ouro.application.runtime.service import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.cli.run import _run_exit_code
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PromptRejectedError
from ouro.core.messages import MessageRecord
from ouro.core.prompt_policies import (
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, Usage
from ouro.local_application import build_local_usage_service
from ouro.providers.faux import FauxProvider
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.tools import ToolRegistry
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


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore], ids=["files", "sqlite"])
def open_input_runtime(tmp_path, request):
    def open_runtime(provider, **options):
        store_type = request.param
        store = store_type(
            tmp_path / "sessions.db" if store_type is SQLiteSessionStore else tmp_path
        )
        store.initialize()
        return RuntimeService(
            store=store,
            adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
            tools=ToolRegistry(),
            **options,
        )

    return open_runtime


async def test_reopened_first_input_rechecks_current_policy(open_input_runtime):
    user_input = UserInputRequest(prompt="run after maintenance")
    ids = {"new_session_id": user_input.new_session_id, "input_id": user_input.input_id}
    decisions = []

    def policies(action):
        registry = PromptPolicyRegistry()

        def decide(call):
            decisions.append((action, call.prospective_run_id))
            return PromptPolicyDecision(
                action=action, reason="maintenance" if action == "deny" else ""
            )

        registry.register(PromptPolicyRegistration("ouro.host", "maintenance", decide))
        return registry

    def interrupt():
        raise InterruptedError("closed after receipt")

    async with open_input_runtime(FauxProvider(), prompt_policies=policies("allow")) as first:
        with pytest.raises(InterruptedError, match="closed after receipt"):
            await first.run(user_input.prompt, **ids, on_input_accepted=interrupt)
    async with open_input_runtime(FauxProvider(), prompt_policies=policies("deny")) as reopened:
        with pytest.raises(PromptRejectedError):
            await reopened.run(user_input.prompt, **ids)
        assert (
            reopened.store.load_input_state(user_input.new_session_id, user_input.input_id).status
            == "pending"
        )
        assert reopened.store.load_runs(user_input.new_session_id) == []
        assert not reopened.store.has_input_messages(user_input.new_session_id)
    async with open_input_runtime(FauxProvider(), prompt_policies=policies("allow")) as allowed:
        result = await allowed.run(user_input.prompt, **ids)
        assert result.run.status == "completed"
        assert decisions == [(action, result.run.run_id) for action in ("allow", "deny", "allow")]
        await allowed.run("new first input")
        assert len(decisions) == 4


async def test_receipt_retry_waits_for_its_own_fifo_input(open_input_runtime):
    async with open_input_runtime(FauxProvider()) as runtime:
        session_id = runtime.create_session().session_id
        for input_id in ("first", "second"):
            await runtime.accept_user_input(
                UserInputRequest(session_id=session_id, input_id=input_id, prompt=input_id)
            )
        result = await runtime.run("second", session_id=session_id, input_id="second")
        runs = runtime.store.load_runs(session_id)
        assert len(runs) == 2
        assert [
            runtime.store.find_queued_input_message(session_id, key).run_id
            for key in ("first", "second")
        ] == [r.run_id for r in runs]
        assert result.run == runs[1]


async def test_failed_receipt_restores_diagnostic_usage_and_exit_code(open_input_runtime, tmp_path):
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent.from_tool_call(
                    {"id": "call_missing", "name": "Missing", "arguments": {}}
                ),
                ProviderStreamEvent(type="done", usage=Usage(input_tokens=5, output_tokens=2)),
            ],
            [
                ProviderStreamEvent(
                    type="error", error="Unavailable.", error_code="authentication_required"
                )
            ],
            [
                ProviderStreamEvent(type="text_delta", text="back online"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    async with open_input_runtime(
        provider, usage_service=build_local_usage_service(tmp_path)
    ) as runtime:
        ids = {"session_id": runtime.create_session().session_id, "input_id": "failed-input"}
        first = await runtime.run("use the tool", **ids)
        assert first.run.status == "failed" and _run_exit_code(first) == 6
        assert first.usage["input_tokens"] == 5 and first.usage["output_tokens"] == 2
        assert first.error_code == "authentication_required" and first.had_tool_error
    async with open_input_runtime(
        provider, usage_service=build_local_usage_service(tmp_path)
    ) as reopened:
        repeated = await reopened.run("use the tool", **ids)
        assert (
            repeated.run,
            repeated.final_text,
            repeated.had_tool_error,
            repeated.error_code,
            repeated.usage,
        ) == (first.run, first.final_text, first.had_tool_error, first.error_code, first.usage)
        assert _run_exit_code(repeated) == 6 and provider._stream_count == 2
        # Current-format Runs without a recorded invocation decision retain native evidence.
        persisted = reopened.store.load_run(ids["session_id"], first.run.run_id)
        metadata = dict(persisted.metadata)
        metadata.pop("invocation_result")
        reopened.store.save_run(persisted.model_copy(update={"metadata": metadata}))
        retained = await reopened.run("use the tool", **ids)
        assert (
            retained.final_text,
            retained.had_tool_error,
            retained.error_code,
            retained.usage,
        ) == (first.final_text, first.had_tool_error, first.error_code, first.usage)
        # A subsequent invocation can finish the same Run without a tool error.
        resumed = await reopened.continue_run(session_id=ids["session_id"])
        assert resumed.run.run_id == first.run.run_id and not resumed.had_tool_error
        assert resumed.run.status == "completed"
        # A writer can preserve the preceding terminal metadata when it continues the Run.
        reopened.store.save_run(
            resumed.run.model_copy(
                update={
                    "metadata": {
                        **resumed.run.metadata,
                        "invocation_result": first.run.metadata["invocation_result"],
                    }
                }
            )
        )
        after_resume = await reopened.run("use the tool", **ids)
        assert (after_resume.final_text, after_resume.had_tool_error, after_resume.usage) == (
            resumed.final_text,
            resumed.had_tool_error,
            resumed.usage,
        )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_supervisor_wait_returns_the_submitted_input_among_pending_units(tmp_path, backend):
    held = HeldProvider()
    app = streaming_application(tmp_path, held, backend=backend)
    app.supervisor.max_concurrency = 1
    async with app:
        blocker = await app.supervisor.submit_user_input(UserInputRequest(prompt="occupy permit"))
        try:
            assert await asyncio.wait_for(held.started.get(), 5) == "occupy permit"
            session_id = app.runtime.create_session().session_id
            submissions = [
                await app.supervisor.submit_user_input(
                    UserInputRequest(session_id=session_id, input_id=key, prompt=key)
                )
                for key in ("a", "b")
            ]
            pending = app.runtime.store.load_session_record(session_id)
            assert pending.active_run_id is None and pending.pending_input_ids == ("a", "b")
        finally:
            held.release.set()
            await blocker.wait()
        first, second = await asyncio.wait_for(
            asyncio.gather(*(submission.wait() for submission in submissions)), 10
        )
        assert first.run.run_id != second.run.run_id
        for key, result in zip(("a", "b"), (first, second), strict=True):
            assert (
                app.runtime.store.find_queued_input_message(session_id, key).run_id
                == result.run.run_id
            )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_first_user_run_preserves_entry_origin(tmp_path, backend):
    async with streaming_application(tmp_path, FauxProvider(), backend=backend) as app:
        direct = await app.runtime.run("first")
        submission = await app.supervisor.submit_user_input(UserInputRequest(prompt="first"))
        queued = await submission.wait()
        for result, origin in ((direct, "user"), (queued, "follow_up")):
            run = app.runtime.store.load_run(result.session.session_id, result.run.run_id)
            assert (run.origin, run.trigger, run.status) == (origin, "user", "completed")


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_direct_retry_finds_the_winning_commit_after_admission(
    tmp_path, backend, monkeypatch
):
    async with streaming_application(tmp_path, FauxProvider(), backend=backend) as app:
        runtime = app.runtime
        session = runtime.create_session()
        destination = {"session_id": session.session_id, "input_id": "same"}
        entered, resume = asyncio.Event(), asyncio.Event()
        load_target = runtime._input_target

        async def wait_after_lookup(session_id):
            entered.set()
            await resume.wait()
            return await load_target(session_id)

        with monkeypatch.context() as delayed:
            delayed.setattr(runtime, "_input_target", wait_after_lookup)
            retry = asyncio.create_task(runtime.run("one input", **destination))
            await asyncio.wait_for(entered.wait(), 5)
        try:
            winner = await runtime.run("one input", **destination)
        finally:
            resume.set()
        repeated = await retry
        assert (repeated.run.run_id, repeated.final_text) == (winner.run.run_id, winner.final_text)
        assert len(runtime.store.load_runs(session.session_id)) == 1


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_direct_first_input_retry_shares_the_running_owner(tmp_path, backend):
    provider = HeldProvider()
    async with streaming_application(tmp_path, provider, backend=backend) as app:
        runtime = app.runtime
        request = UserInputRequest(prompt="first")
        ids = {"new_session_id": request.new_session_id, "input_id": request.input_id}
        task = asyncio.create_task(runtime.run(request.prompt, **ids))
        try:
            assert await asyncio.wait_for(provider.started.get(), 5) == request.prompt
            owners = set(runtime._run_resources)
            retry = await runtime.run(request.prompt, **ids)
            assert retry.run.status == "running"
            assert set(runtime._run_resources) == owners == {retry.run.run_id}
            assert provider.active == provider.maximum == 1
        finally:
            provider.release.set()
            completed = await task
        assert completed.run.run_id == retry.run.run_id
        assert not runtime._run_resources


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
        assert queued[0].role == "user"
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
    accept = app.runtime.input_admission.accept_initial

    def held_accept(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return accept(*args)

    monkeypatch.setattr(app.runtime.input_admission, "accept_initial", held_accept)
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
    accept = app.runtime.input_admission.accept

    async def held_accept(request):
        entered.set()
        await release.wait()
        return await accept(request)

    monkeypatch.setattr(app.runtime.input_admission, "accept", held_accept)
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

    monkeypatch.setattr(app.runtime, "run_pending_inputs", fail)
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


async def test_initial_retry_returns_receipt_and_head_from_one_read(
    open_input_runtime, monkeypatch
):
    runtime = open_input_runtime(FauxProvider())
    request = UserInputRequest(prompt="shared first input")
    admission = runtime.input_admission
    accept_new = admission.accept_new
    first_waiting, published = asyncio.Event(), asyncio.Event()

    async def interleaved(value):
        if first_waiting.is_set():
            initial = await accept_new(value)
            published.set()
            return initial
        first_waiting.set()
        await published.wait()
        duplicate = await accept_new(value)
        result = await runtime.run(
            value.prompt, new_session_id=value.new_session_id, input_id=value.input_id
        )
        assert result.run.status == "completed"
        return duplicate

    monkeypatch.setattr(admission, "accept_new", interleaved)
    async with runtime:
        retry = asyncio.create_task(admission.accept(request))
        await first_waiting.wait()
        await runtime.accept_user_input(request)
        receipt, head = await retry
        assert receipt.run_id is None
        assert head.pending_input_ids == (receipt.queue_id,)
        consumed = await runtime.accept_user_input(request)
        assert consumed.run_id is not None


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
