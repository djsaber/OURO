from __future__ import annotations

import asyncio

import pytest

from ouro.application.contracts.execution import ShutdownPolicy
from ouro.application.inputs.requests import UserInputRequest
from ouro.core.prompt_policies import PromptPolicyDecision, PromptPolicyRegistration
from ouro.providers import FauxProvider
from ouro.tui.controller import TuiController
from tests.application.helpers import running_runtime_task, wait_for_session_state
from tests.application.test_queued_input_modes import _runtime
from tests.application.test_supervisor import ConcurrencyProvider
from tests.event_capture import recorded_events
from tests.tui.app.support import _approval_application
from tests.tui.test_tasks import streaming_application


async def test_standalone_successor_rejection_preserves_completed_result_and_input(tmp_path):
    store, provider, runtime = _runtime(tmp_path, consumption_mode="one_by_one")
    denied = False
    evaluations = []

    def guard(call):
        if call.prompt == "later":
            evaluations.append(denied)
            if denied:
                return PromptPolicyDecision(action="deny", reason="delivery denied")
        return PromptPolicyDecision()

    runtime.prompt_policies.register(PromptPolicyRegistration("ouro.host", "guard", guard))
    session = runtime.create_session()
    async with running_runtime_task(
        runtime, runtime.run("initial", session_id=session.session_id)
    ) as running:
        await provider.first_started.wait()
        await runtime.run("later", session_id=session.session_id)
        pending = store.load_session_record(session.session_id).pending_input_ids
        denied = True
        provider.release_first.set()
        result = await running
        assert result.run.status == "completed"
        assert result.final_text == "answer-1"
        assert len(store.load_runs(session.session_id)) == 1
        assert len(provider.contexts) == 1
        assert store.load_session_record(session.session_id).pending_input_ids == pending
        assert evaluations == [False, True]
        diagnostics = [
            event.payload
            for event in recorded_events(session.session_id)
            if event.event_type == "diagnostic"
            and event.payload.get("code") == "session_dispatch_failed"
        ]
        assert len(diagnostics) == 1
        assert diagnostics[0]["error_type"] == "PromptRejectedError"

        denied = False
        await runtime.advance_session(session.session_id, session.history_epoch)
        assert store.load_input_state(session.session_id, pending[0]).status == "consumed"
        assert [run.status for run in store.load_runs(session.session_id)] == [
            "completed",
            "completed",
        ]
        assert len(provider.contexts) == 2


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_scheduling_an_empty_queue_does_not_load_execution_state(
    tmp_path, monkeypatch, backend
):
    app = streaming_application(tmp_path, FauxProvider(), backend=backend)
    session = app.runtime.create_session()

    def forbidden(*args, **kwargs):
        pytest.fail("an empty queue loaded execution state")

    monkeypatch.setattr(app.runtime.store, "load_runtime_session", forbidden)
    try:
        await app.supervisor.schedule_session(session.session_id)
        await app.supervisor._session_dispatch_tasks[session.session_id]
        assert app.runtime.store.load_session_record(session.session_id) == session
    finally:
        await app.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("change", ["cancel_latest", "retry_consumed"])
async def test_session_dispatch_survives_changes_to_the_latest_receipt(
    tmp_path, monkeypatch, backend, change
):
    app = streaming_application(tmp_path, FauxProvider(), backend=backend)
    runtime, supervisor = app.runtime, app.supervisor
    finished, release = asyncio.Event(), asyncio.Event()
    run = runtime.run

    async def held_run(*args, **kwargs):
        result = await run(*args, **kwargs)
        if not finished.is_set():
            finished.set()
            await release.wait()
        return result

    monkeypatch.setattr(runtime, "run", held_run)
    try:
        initial = UserInputRequest(prompt="initial")
        first = await supervisor.submit_user_input(initial)
        await asyncio.wait_for(finished.wait(), 10)
        session_id = first.receipt.session_id
        pending = await supervisor.submit_user_input(
            UserInputRequest(prompt="keep pending", session_id=session_id)
        )
        if change == "cancel_latest":
            last = await supervisor.submit_user_input(
                UserInputRequest(prompt="withdraw this", session_id=session_id)
            )
            await runtime.cancel_pending_user_input(session_id, last.receipt.queue_id)
        else:
            await supervisor.submit_user_input(initial)
        release.set()
        await first.wait()
        message = await wait_for_session_state(
            supervisor,
            session_id,
            lambda: runtime.store.find_queued_input_message(session_id, pending.receipt.queue_id),
            event_types=frozenset({"queued_input_consumed"}),
        )
        assert message.text_content == "keep pending"
        assert not runtime.pending_user_inputs(session_id)
        assert [
            m.text_content
            for m in runtime.store.load_session(session_id).messages
            if m.role == "user"
        ] == ["initial", "keep pending"]
    finally:
        release.set()
        await app.close()


async def test_compaction_release_schedules_accepted_session_input(tmp_path, monkeypatch):
    app = streaming_application(tmp_path, FauxProvider())
    runtime, supervisor = app.runtime, app.supervisor
    entered, release = asyncio.Event(), asyncio.Event()
    compact = runtime.context_compaction.compact

    async def held_compact(*args, **kwargs):
        entered.set()
        await release.wait()
        return await compact(*args, **kwargs)

    monkeypatch.setattr(runtime.context_compaction, "compact", held_compact)
    try:
        first = await supervisor.submit_user_input(UserInputRequest(prompt="initial"))
        await first.wait()
        session_id = first.receipt.session_id
        task = asyncio.create_task(runtime.compact(session_id=session_id))
        await asyncio.wait_for(entered.wait(), 10)
        pending = await supervisor.submit_user_input(
            UserInputRequest(prompt="during compaction", session_id=session_id)
        )
        release.set()
        await task
        await wait_for_session_state(
            supervisor,
            session_id,
            lambda: runtime.store.find_queued_input_message(session_id, pending.receipt.queue_id),
            event_types=frozenset({"queued_input_consumed"}),
        )
        assert not runtime.pending_user_inputs(session_id)
    finally:
        release.set()
        await app.close()


async def test_control_selection_adopts_accepted_input_without_a_run(tmp_path):
    app = streaming_application(tmp_path, FauxProvider())
    receipt = await app.runtime.accept_user_input(
        UserInputRequest(prompt="accepted before restart")
    )
    await app.close()
    app = streaming_application(tmp_path, FauxProvider())
    controller = TuiController(lambda event: None, lambda status: None, application=app)
    try:
        await controller.start()
        await controller.load_session(receipt.session_id)
        await wait_for_session_state(
            app.supervisor,
            receipt.session_id,
            lambda: app.runtime.store.find_queued_input_message(
                receipt.session_id, receipt.queue_id
            ),
            event_types=frozenset({"queued_input_consumed"}),
        )
        assert not app.runtime.pending_user_inputs(receipt.session_id)
    finally:
        await controller.close()


async def test_follow_up_reenters_shared_execution_capacity(tmp_path):
    provider = ConcurrencyProvider()
    app = streaming_application(tmp_path, provider)
    app.supervisor.max_concurrency = 1
    try:
        first = await app.supervisor.submit_user_input(UserInputRequest(prompt="first"))
        assert await asyncio.wait_for(provider.starts.get(), 10) == "first"
        await app.supervisor.submit_user_input(
            UserInputRequest(prompt="follow up", session_id=first.receipt.session_id)
        )
        second = await app.supervisor.submit_user_input(UserInputRequest(prompt="other session"))
        provider.release.set()
        await asyncio.gather(first.wait(), second.wait())
        assert await asyncio.wait_for(provider.starts.get(), 10) == "other session"
        assert await asyncio.wait_for(provider.starts.get(), 10) == "follow up"
        assert provider.maximum == 1
    finally:
        provider.release.set()
        await app.close()


async def test_continue_consumes_accepted_input_after_preparation_failure(tmp_path, monkeypatch):
    app = streaming_application(tmp_path, FauxProvider())
    consume = app.runtime.run_pending_inputs

    async def fail(*args, **kwargs):
        raise OSError("preparation unavailable")

    monkeypatch.setattr(app.runtime, "run_pending_inputs", fail)
    try:
        submission = await app.supervisor.submit_user_input(UserInputRequest(prompt="retained"))
        with pytest.raises(OSError, match="preparation unavailable"):
            await submission.wait()
        monkeypatch.setattr(app.runtime, "run_pending_inputs", consume)
        handle = await app.supervisor.continue_run(session_id=submission.receipt.session_id)
        result = await app.supervisor.wait(handle)
        assert result.status == "completed"
        message = app.runtime.store.find_queued_input_message(
            submission.receipt.session_id, submission.receipt.queue_id
        )
        assert message.run_id == result.run_id
        assert not app.runtime.pending_user_inputs(submission.receipt.session_id)
        app.supervisor.release_handle(handle)
    finally:
        await app.close()


async def test_parked_abort_releases_the_session_to_pending_input(tmp_path):
    app = _approval_application(tmp_path, tmp_path / "output.txt")
    try:
        first = await app.supervisor.submit_user_input(UserInputRequest(prompt="initial"))
        waiting = await first.wait()
        assert waiting.run.status == "waiting_approval"
        follow_up = await app.supervisor.submit_user_input(
            UserInputRequest(prompt="next work", session_id=first.receipt.session_id)
        )
        await app.supervisor.abort(first.receipt.session_id)
        message = await wait_for_session_state(
            app.supervisor,
            first.receipt.session_id,
            lambda: app.runtime.store.find_queued_input_message(
                first.receipt.session_id, follow_up.receipt.queue_id
            ),
            event_types=frozenset({"queued_input_consumed"}),
        )
        assert message.run_id != waiting.run.run_id
        assert (
            app.runtime.store.load_run(first.receipt.session_id, waiting.run.run_id).status
            == "aborted"
        )
    finally:
        await app.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_accepting_host_keeps_progress_when_the_execution_host_closes(tmp_path, backend):
    provider = ConcurrencyProvider()
    owner = streaming_application(tmp_path, provider, backend=backend)
    receiver = streaming_application(tmp_path, FauxProvider(), backend=backend)
    try:
        first = await owner.supervisor.submit_user_input(UserInputRequest(prompt="owned elsewhere"))
        await asyncio.wait_for(provider.starts.get(), 10)
        pending = await receiver.supervisor.submit_user_input(
            UserInputRequest(prompt="accepted here", session_id=first.receipt.session_id)
        )
        await owner.supervisor.shutdown(ShutdownPolicy(mode="abort"))
        message = await wait_for_session_state(
            receiver.supervisor,
            first.receipt.session_id,
            lambda: receiver.runtime.store.find_queued_input_message(
                first.receipt.session_id, pending.receipt.queue_id
            ),
            event_types=frozenset({"queued_input_consumed"}),
        )
        assert message.text_content == "accepted here"
        assert not receiver.runtime.pending_user_inputs(first.receipt.session_id)
    finally:
        provider.release.set()
        await owner.close()
        await receiver.close()


async def test_one_by_one_selection_and_commit_read_only_the_selected_input(tmp_path, monkeypatch):
    app = streaming_application(tmp_path, FauxProvider())
    try:
        first = await app.runtime.accept_user_input(UserInputRequest(prompt="first"))
        for index in range(8):
            await app.runtime.accept_user_input(
                UserInputRequest(prompt=str(index), session_id=first.session_id)
            )
        reads = []
        load = app.runtime.store.load_input

        def read(session_id, input_id):
            reads.append(input_id)
            return load(session_id, input_id)

        monkeypatch.setattr(app.runtime.store, "load_input", read)
        snapshot = app.runtime.store.load_runtime_session(first.session_id)
        selected = await app.runtime._pending_queued_inputs(snapshot)
        with app.runtime.store.session_mutation(first.session_id):
            assert app.runtime.queued_inputs.matches_pending(snapshot.session, selected)
        assert reads == [first.queue_id, first.queue_id]
    finally:
        await app.close()
