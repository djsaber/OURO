from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path

import pytest

from ouro.core.state import SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolResult
from tests.application.tool_runtime import runtime


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
    asyncio.run(app.run("execute once", session_id=session_id))
    raise AssertionError("process did not reach the intended durable boundary")


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize(
    "boundary", ["before_start", "after_start", "before_outcome", "after_outcome"]
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
    expected_effects = 1 if boundary in {"before_outcome", "after_outcome"} else 0

    def effect_count():
        return effects.read_bytes().splitlines().count(b"effect") if effects.exists() else 0

    assert effect_count() == expected_effects
    cold = _store(root, backend)
    cold.initialize()
    app = runtime(cold, _tool(effects), {})
    # A fresh model can finish after recovering the existing Call; it does not
    # propose another external action that could be confused with a replay.
    app.adapter.provider = FauxProvider()
    try:
        for _ in range(2):
            recovered = await app.reconcile_execution_session(session.session_id)
            await app.run_recovery.reconcile(recovered)
            assert effect_count() == expected_effects
        snapshot = cold.load_session(session.session_id)
        call = snapshot.tool_facts.calls[0]
        start = cold.load_tool_call_start(session.session_id, call.identity, 1)
        outcome = cold.load_tool_outcome(session.session_id, call.identity, 1)
        state = cold.load_tool_call_state(session.session_id, call.identity)
        assert (start is None) == (boundary == "before_start")
        assert (outcome is not None) == (boundary == "after_outcome")
        if boundary == "before_start":
            assert state.status == "aborted"
        elif boundary in {"after_start", "before_outcome"}:
            assert state.status == "unknown_outcome"
            feedback = cold.load_message(session.session_id, state.result_message_id)
            assert feedback.outcome_ref.outcome_attempt is None
            assert "outcome is unknown" in feedback.text_content
        elif boundary == "after_outcome":
            assert outcome.value.content == "external effect confirmed"
            feedback = cold.load_message(session.session_id, state.result_message_id)
            assert feedback.outcome_ref.outcome_attempt == 1
        resumed = await app.continue_run(session_id=session.session_id, run_id=call.identity.run_id)
        assert resumed.run.status == "completed", resumed.run.error
        assert effect_count() == expected_effects
    finally:
        await app.close()
