from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path

import pytest

from ouro.core.state import SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v7 as sdk
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolInputRequest, ToolRegistry, ToolResult
from tests.application.tool_runtime import batch_runtime, runtime


def _store(root, backend):
    return (
        LocalSessionStore(root)
        if backend == "files"
        else SQLiteSessionStore(Path(root) / "sessions.db")
    )


def _tool(effects, *, stop_before_effect=False):
    def execute(invocation, context):
        if stop_before_effect:
            os._exit(37)
        with Path(effects).open("ab") as target:
            target.write(b"effect\n")
            target.flush()
            os.fsync(target.fileno())
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="external effect confirmed",
        )

    return Tool(
        definition=ToolDefinition(
            name="External", description="Record effect", effect="state_changing"
        ),
        handler=execute,
    )


def _crash_process(root, backend, session_id, effects, boundary):
    store = _store(root, backend)
    store.initialize()
    app = runtime(store, _tool(effects, stop_before_effect=boundary == "after_start"), {})
    commit = store.commit_session

    def interrupted(request):
        if boundary == "before_start" and request.tool_facts.starts:
            os._exit(37)
        if boundary == "before_outcome" and request.tool_facts.outcomes:
            os._exit(37)
        return commit(request)

    store.commit_session = interrupted
    if boundary == "after_outcome":
        save_result = app.run_lifecycle.save_tool_result

        def after_observation(**kwargs):
            save_result(**kwargs)
            os._exit(37)

        app.run_lifecycle.save_tool_result = after_observation
    if boundary == "after_frame":
        publish = app.run_lifecycle.publish_tool_batch

        def after_publication(**kwargs):
            publish(**kwargs)
            os._exit(37)

        app.run_lifecycle.publish_tool_batch = after_publication
    asyncio.run(app.run("execute once", session_id=session_id))
    raise AssertionError("process did not reach the intended durable boundary")


def _exit_with_question_and_inflight_effect(
    root, backend, session_id, effects, known_result, question_saved, handoff
):
    entered = asyncio.Event()

    async def question(invocation, context):
        await entered.wait()
        return ToolInputRequest(data={"prompt": "Continue?"})

    async def external(invocation, context):
        with Path(effects).open("ab") as target:
            target.write(b"effect\n")
        entered.set()
        await asyncio.Event().wait()

    registry = ToolRegistry()
    calls = []
    if known_result:

        def observe(call):
            call.session_state.replace(state_schema_version=1, payload={"step": "observed"})
            return sdk.ToolOutput(content="confirmed observation")

        registry.register(
            adapt_sdk_contributions(
                sdk.ContributionSet(
                    tools=[
                        sdk.ToolContribution(
                            id="observe",
                            exposed_name="Observe",
                            description="Observe and propose state.",
                            handler=observe,
                            permission_mode="auto",
                        )
                    ]
                ),
                owner_id="example.observe",
            ).tools[0]
        )
        calls.append(ToolCallRequest(id="known", name="Observe"))
    for name, handler in (("Question", question), ("External", external)):
        registry.register(
            Tool(definition=ToolDefinition(name=name, description=name), handler=handler)
        )
    app = batch_runtime(
        Path(root),
        registry=registry,
        mode="parallel",
        store_type=LocalSessionStore if backend == "files" else SQLiteSessionStore,
        first_calls=[
            *calls,
            ToolCallRequest(id="q", name="Question", source_index=len(calls)),
            ToolCallRequest(id="effect", name="External", source_index=len(calls) + 1),
        ],
    )

    def exit_after_commit(event):
        if event.event_type == "ask_user_requested":
            question_saved.set()
            assert handoff.wait(30)
            os._exit(37)

    app.add_event_sink(exit_after_commit)
    asyncio.run(app.run("ask and write", session_id=session_id))


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize(
    ("legacy", "known_result", "answer_before_exit"),
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
    ids=["current", "legacy-waiting-run", "with-observed-proposal", "owner-dies-during-answer"],
)
async def test_cold_answer_preserves_question_and_reports_inflight_sibling_once(
    tmp_path, backend, legacy, known_result, answer_before_exit, monkeypatch
):
    root, effects = tmp_path / "home", tmp_path / "external.txt"
    store = _store(root, backend)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    ctx = multiprocessing.get_context("spawn")
    question_saved, handoff = ctx.Event(), ctx.Event()
    process = ctx.Process(
        target=_exit_with_question_and_inflight_effect,
        args=(root, backend, session.session_id, effects, known_result, question_saved, handoff),
    )
    process.start()
    try:
        if not answer_before_exit:
            handoff.set()
        assert await asyncio.to_thread(question_saved.wait, 30)
        if not answer_before_exit:
            await asyncio.to_thread(process.join, 30)
            assert process.exitcode == 37
        cold = _store(root, backend)
        cold.initialize()
        if answer_before_exit:
            acquire = cold.acquire_session_lock

            def acquire_after_crash(*args):
                lock = acquire(*args)
                if lock is None:
                    handoff.set()
                    process.join(10)
                    assert process.exitcode == 37
                return lock

            monkeypatch.setattr(cold, "acquire_session_lock", acquire_after_crash)
        run_id = cold.load_session_record(session.session_id).last_run_id
        saved = cold.load_run(session.session_id, run_id)
        assert saved.active_tool_batch.phase == "executing"
        assert saved.status == "running"
        if legacy:
            cold.save_run(
                saved.model_copy(update={"status": "waiting_user", "phase": "human_interaction"})
            )
        async with runtime(cold, _tool(effects), {}) as app:
            app.adapter.provider = FauxProvider()
            finished = await app.answer_user(
                session_id=session.session_id, run_id=run_id, answer="yes"
            )
            assert finished.run.status == "completed", finished.run.error
            messages = [m for m in cold.load_session(session.session_id).messages if m.tool_result]
            if known_result:
                known = messages.pop(0)
                assert known.tool_result.tool_call_id == "known"
                assert known.tool_feedback.error_type == "ToolBusinessEffectsNotApplied"
                outcome = cold.load_tool_outcome(session.session_id, known.outcome_ref.identity, 1)
                assert outcome.value.content == "confirmed observation"
                assert not cold.load_harness(session.session_id).state.states
            assert [m.tool_result.tool_call_id for m in messages] == ["q", "effect"]
            assert messages[0].text_content == "yes"
            assert messages[1].outcome_ref.outcome_attempt is None
            assert "outcome is unknown" in messages[1].text_content
            assert effects.read_bytes() == b"effect\n"
            await asyncio.to_thread(process.join, 5)
            assert process.exitcode == 37
    finally:
        handoff.set()
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize(
    "boundary", ["before_start", "after_start", "before_outcome", "after_outcome", "after_frame"]
)
async def test_cold_process_recovery_distinguishes_unstarted_unknown_and_observed_effects(
    tmp_path, backend, boundary
):
    root, effects = tmp_path / "home", tmp_path / "external-target.txt"
    store = _store(root, backend)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_process,
        args=(str(root), backend, session.session_id, str(effects), boundary),
    )
    process.start()
    try:
        await asyncio.to_thread(process.join, 20)
        assert process.exitcode == 37
    finally:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
    expected_effects = 1 if boundary in {"before_outcome", "after_outcome", "after_frame"} else 0

    def effect_count():
        return effects.read_bytes().splitlines().count(b"effect") if effects.exists() else 0

    assert effect_count() == expected_effects
    cold = _store(root, backend)
    cold.initialize()
    app = runtime(cold, _tool(effects), {})
    app.adapter.provider = FauxProvider()
    try:
        initial = cold.load_session(session.session_id)
        before_recovery = [m for m in initial.messages if m.outcome_ref is not None]
        assert len(before_recovery) == int(boundary == "after_frame")
        for _ in range(2):
            recovered = await app.reconcile_execution_session(session.session_id)
            await app.run_recovery.reconcile(recovered)
            assert effect_count() == expected_effects
        snapshot = cold.load_session(session.session_id)
        call = snapshot.tool_facts.calls[0]
        start = cold.load_tool_call_start(session.session_id, call.identity, 1)
        outcome = cold.load_tool_outcome(session.session_id, call.identity, 1)
        state = cold.load_tool_call_state(session.session_id, call.identity)
        if before_recovery:
            assert state.result_message_id == before_recovery[0].message_id
            assert cold.load_message(session.session_id, state.result_message_id).outcome_ref == (
                before_recovery[0].outcome_ref
            )
        assert (start is None) == (boundary == "before_start")
        assert (outcome is not None) == (boundary in {"after_outcome", "after_frame"})
        if boundary == "before_start":
            assert state.status == "aborted"
        elif boundary in {"after_start", "before_outcome"}:
            assert state.status == "unknown_outcome"
            feedback = cold.load_message(session.session_id, state.result_message_id)
            assert feedback.outcome_ref.outcome_attempt is None
            assert "outcome is unknown" in feedback.text_content
        else:
            assert outcome.value.content == "external effect confirmed"
            feedback = cold.load_message(session.session_id, state.result_message_id)
            assert feedback.outcome_ref.outcome_attempt == 1
        resumed = await app.continue_run(session_id=session.session_id, run_id=call.identity.run_id)
        assert resumed.run.status == "completed", resumed.run.error
        assert effect_count() == expected_effects
    finally:
        await app.close()
