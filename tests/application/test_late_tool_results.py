from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from ouro.core.harness_calls import HarnessInvocation
from ouro.core.session_commits import SessionCommit
from ouro.core.session_controls import SessionControlTransitionRequest
from ouro.core.state import SessionRecord
from ouro.core.tools import ToolDefinition, ToolResult
from ouro.plugins.catalog.bindings import bind_generation_gate
from ouro.plugins.catalog.gates import GenerationGate
from ouro.plugins.contributions.models import ContributionOwner, PreparedContributionSet
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.worker.instance import WorkerPluginInstance
from ouro.sdk.plugins import v5 as sdk
from ouro.tools import Tool
from tests.application.tool_runtime import runtime
from tests.application.tool_runtime import store as store


@pytest.mark.parametrize("callback_kind", ["sync", "async"])
@pytest.mark.parametrize("sdk_callback", [False, True])
@pytest.mark.parametrize("cancellation", ["abort", "task", "withdraw", "generation"])
async def test_late_callback_result_retains_only_original_observation(
    store, tmp_path, monkeypatch, callback_kind, sdk_callback, cancellation
):
    import ouro.core.callbacks as callback_module
    import ouro.tools.base as tool_module

    entered = asyncio.Event()
    closed = asyncio.Event()
    settling = asyncio.Event()
    release_sync = threading.Event()
    release_async = asyncio.Event()
    loop = asyncio.get_running_loop()
    effects = tmp_path / "external-effect.txt"
    settle = tool_module.settle_owned_tasks
    close = HarnessInvocation.close

    async def observe_settlement(*tasks):
        settling.set()
        return await settle(*tasks)

    def observe_close(view):
        close(view)
        closed.set()

    monkeypatch.setattr(tool_module, "settle_owned_tasks", observe_settlement)
    monkeypatch.setattr(callback_module, "settle_owned_tasks", observe_settlement)
    monkeypatch.setattr(HarnessInvocation, "close", observe_close)

    def begin(call):
        with effects.open("a", encoding="utf-8") as stream:
            stream.write("performed\n")
        if sdk_callback:
            call.session_state.replace(state_schema_version=1, payload={"unaccepted": True})
        loop.call_soon_threadsafe(entered.set)

    def finish(call):
        if sdk_callback:
            with pytest.raises(RuntimeError, match="closed"):
                call.session_state.replace(state_schema_version=1, payload={"late": True})
            return sdk.ToolOutput(
                content="actual late result",
                structured={"external_effect": "performed"},
                session_control_transitions=[
                    sdk.SessionControlTransition(action="acquire", controller_id="unavailable")
                ],
            )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            content="actual late result",
            structured={"external_effect": "performed"},
            session_control_transitions=[
                SessionControlTransitionRequest(action="acquire", controller_id="unavailable")
            ],
        )

    def sync_handler(call, context):
        begin(call)
        assert release_sync.wait(60)
        return finish(call)

    async def async_handler(call, context):
        begin(call)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_async.wait()
        return finish(call)

    handler = sync_handler if callback_kind == "sync" else async_handler
    if sdk_callback:
        tool = adapt_sdk_contributions(
            sdk.ContributionSet(
                tools=[
                    sdk.ToolContribution(
                        id="late",
                        exposed_name="Late",
                        description="Return after cancellation.",
                        permission_mode="auto",
                        effect="state_changing",
                        handler=handler,
                    )
                ]
            ),
            owner_id="example.late",
        ).tools[0]
    else:
        tool = Tool(
            definition=ToolDefinition(name="Late", description="Return after cancellation."),
            handler=handler,
        )
    gate = GenerationGate("late-test")
    tool = bind_generation_gate(
        PreparedContributionSet(
            owner=ContributionOwner("example.late", "user", "test", "late-test"), tools=(tool,)
        ),
        gate,
    ).tools[0]
    app = runtime(store, tool, {})
    session = SessionRecord()
    store.create_session(session)
    execution = asyncio.create_task(app.run("perform once", session_id=session.session_id))
    abort = None
    try:
        await asyncio.wait_for(entered.wait(), 60)
        closed.clear()
        settling.clear()
        current = store.load_session_record(session.session_id)
        run = store.load_run(session.session_id, current.active_run_id)
        identity = run.active_tool_batch.identity(run.run_id, "call_state")
        start = store.load_tool_call_start(session.session_id, identity, 1)
        assert start is not None
        assert store.load_tool_outcome(session.session_id, identity, 1) is None
        if cancellation == "abort":
            abort = asyncio.create_task(app.abort_run(session_id=session.session_id))
        elif cancellation == "generation":
            gate.close()
            gate.cancel_inflight()
        else:
            if cancellation == "withdraw":
                first = store.load_session(session.session_id).messages[0]
                store.commit_session(
                    SessionCommit(
                        before=current,
                        before_message_id=first.message_id,
                        after=current.model_copy(update={"status": "idle", "active_run_id": None}),
                        runs=(run.model_copy(update={"status": "aborted"}),),
                    )
                )
            execution.cancel()
        await asyncio.wait_for(settling.wait(), 60)
        if sdk_callback:
            await asyncio.wait_for(closed.wait(), 60)
        assert not execution.done()
        release_sync.set()
        release_async.set()
        if cancellation in {"abort", "generation"}:
            result = await asyncio.wait_for(execution, 60)
            assert result.run.status == ("aborted" if cancellation == "abort" else "completed")
            if abort is not None:
                await asyncio.wait_for(abort, 60)
            else:
                assert execution.cancelling() == 0
        elif cancellation == "task":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(execution, 60)
        else:
            # The durable withdrawal can be observed before task cancellation.
            try:
                result = await asyncio.wait_for(execution, 60)
            except asyncio.CancelledError:
                pass
            else:
                assert result.run.status == "aborted"
        outcome = store.load_tool_outcome(session.session_id, identity, 1)
        assert outcome is not None
        assert outcome.value.content == "actual late result"
        assert outcome.value.structured == {"external_effect": "performed"}
        assert outcome.value.started_at is not None
        assert outcome.value.ended_at is not None
        assert not outcome.value.is_error
        assert effects.read_text(encoding="utf-8") == "performed\n"
        assert not store.load_harness(session.session_id).state.states
        assert not store.load_harness(session.session_id).state.controls.activations
        messages = store.load_session(session.session_id).messages
        if cancellation == "generation":
            feedback = next(message for message in messages if message.role == "tool_result")
            assert feedback.outcome_ref.outcome_attempt is None
            assert feedback.tool_result.is_error
            assert "actual late result" not in feedback.text_content
        else:
            assert all(message.role != "tool_result" for message in messages)
        current = store.load_session_record(session.session_id)
        if cancellation == "withdraw":
            assert current.history_epoch == session.history_epoch + 1
            assert not messages
            assert current.active_run_id is None
        assert store.load_tool_call_start(session.session_id, identity, 1) == start
        assert gate.in_flight == 0
    finally:
        release_sync.set()
        release_async.set()
        execution.cancel()
        await asyncio.gather(
            execution, *([abort] if abort is not None else []), return_exceptions=True
        )
        await app.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("withdraw", [False, True])
@pytest.mark.parametrize("completion", ["reply", "exit"])
async def test_worker_late_result_survives_revocation_without_reverse_call_authority(
    store, tmp_path, asynchronous, withdraw, completion
):
    package = Path(__file__).parents[2] / "examples/plugins/worker-tool"
    raw = json.loads((package / ".ouro-plugin/plugin.json").read_text(encoding="utf-8"))
    raw["runtime"]["command"] = ["{python}", "late_tool_worker.py"]
    raw["contributes"]["tools"][0]["permission_mode"] = "auto"
    manifest = PluginManifest.model_validate(raw)
    entered = asyncio.Event()

    def diagnostic(code, message):
        if code == "entered":
            entered.set()

    worker = WorkerPluginInstance(
        manifest,
        package_root=Path(__file__).parents[1] / "plugins/fixtures",
        package_digest="test",
        permissions=frozenset(),
        configuration={},
        data_directory=tmp_path / "worker",
        credentials={},
        diagnostic=diagnostic,
    )
    reverse_calls = []

    async def host(method, params):
        reverse_calls.append(method)
        raise AssertionError("cancelled invocation authorized a Host call")

    worker.client.request_handler = host
    app = execution = None
    try:
        contributions = await worker.prepare()
        tool = adapt_sdk_contributions(contributions, owner_id=manifest.plugin_id).tools[0]
        effect_file = tmp_path / "worker-effect.txt"
        app = runtime(store, tool, {"effect_file": str(effect_file), "asynchronous": asynchronous})
        session = SessionRecord()
        store.create_session(session)
        execution = asyncio.create_task(app.run("perform once", session_id=session.session_id))
        await asyncio.wait_for(entered.wait(), 60)
        current = store.load_session_record(session.session_id)
        run = store.load_run(session.session_id, current.active_run_id)
        identity = run.active_tool_batch.identity(run.run_id, "call_state")
        if withdraw:
            first = store.load_session(session.session_id).messages[0]
            store.commit_session(
                SessionCommit(
                    before=current,
                    before_message_id=first.message_id,
                    after=current.model_copy(update={"status": "idle", "active_run_id": None}),
                    runs=(run.model_copy(update={"status": "aborted"}),),
                )
            )
        execution.cancel()
        assert await worker.client.request("test.closed")
        assert not execution.done()
        if completion == "reply":
            assert await worker.client.request("test.release")
        else:
            await worker.stop("shutdown", 0)
        try:
            result = await asyncio.wait_for(execution, 60)
        except asyncio.CancelledError:
            pass
        else:
            assert withdraw and result.run.status == "aborted"
        outcome = store.load_tool_outcome(session.session_id, identity, 1)
        if completion == "reply":
            assert outcome is not None
            assert outcome.value.content == "worker actual late result"
            assert outcome.value.structured == {
                "external_effect": "performed",
                "reverse_call_rejected": True,
            }
        else:
            assert outcome is None
        assert effect_file.read_text(encoding="utf-8") == "performed\n"
        assert not reverse_calls
        assert not worker.client._pending
        assert not worker.client._pending_contexts
        assert not worker.client._incoming
        state = store.load_harness(session.session_id).state
        assert not state.states
        assert not state.controls.activations
        snapshot = store.load_session(session.session_id)
        assert all(message.role != "tool_result" for message in snapshot.messages)
        if withdraw:
            assert snapshot.session.history_epoch == session.history_epoch + 1
            assert not snapshot.messages
            assert snapshot.session.active_run_id is None
    finally:
        await worker.stop("shutdown", 0)
        if execution is not None:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        if app is not None:
            await app.close()


@pytest.mark.parametrize("interaction_mode", ["interactive", "unattended"])
async def test_late_resume_result_uses_its_started_attempt(store, monkeypatch, interaction_mode):
    entered = asyncio.Event()
    closed = asyncio.Event()
    release = asyncio.Event()
    close = HarnessInvocation.close

    def observe_close(view):
        close(view)
        if entered.is_set():
            closed.set()

    monkeypatch.setattr(HarnessInvocation, "close", observe_close)

    def request(call):
        call.session_state.replace(state_schema_version=1, payload={"stage": "waiting"})
        return sdk.ToolInputRequest(data={"question": "Continue?"})

    async def resume(call):
        assert call.session_state.read().payload == {"stage": "waiting"}
        call.session_state.replace(state_schema_version=1, payload={"stage": "unaccepted"})
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        with pytest.raises(RuntimeError, match="closed"):
            call.session_state.read()
        return sdk.ToolOutput(content="late resumed result", structured={"resolved": True})

    tool = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="resume",
                    exposed_name="Resume",
                    description="Resume once.",
                    permission_mode="auto",
                    effect="state_changing",
                    batch_behavior="exclusive",
                    handler=request,
                    resume_handler=resume,
                    supports_unattended=True,
                )
            ]
        ),
        owner_id="example.resume",
    ).tools[0]
    app = runtime(store, tool, {})
    session = SessionRecord(interaction_mode=interaction_mode)
    store.create_session(session)
    if interaction_mode == "interactive":
        pending = await app.run("resume once", session_id=session.session_id)
        assert pending.run.status == "waiting_user"
        execution = asyncio.create_task(
            app.answer_user(session_id=session.session_id, run_id=pending.run.run_id, answer="yes")
        )
    else:
        execution = asyncio.create_task(app.run("resume once", session_id=session.session_id))
    try:
        await asyncio.wait_for(entered.wait(), 60)
        current = store.load_session_record(session.session_id)
        run = store.load_run(session.session_id, current.active_run_id)
        identity = run.active_tool_batch.identity(run.run_id, "call_state")
        assert store.load_tool_call_start(session.session_id, identity, 2) is not None
        execution.cancel()
        await asyncio.wait_for(closed.wait(), 60)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution, 60)
        assert store.load_tool_outcome(session.session_id, identity, 1) is None
        outcome = store.load_tool_outcome(session.session_id, identity, 2)
        assert outcome is not None
        assert outcome.value.content == "late resumed result"
        assert outcome.value.structured == {"resolved": True}
        assert store.load_harness(session.session_id).state.states[0].payload == {
            "stage": "waiting"
        }
        assert all(
            message.role != "tool_result"
            for message in store.load_session(session.session_id).messages
        )
    finally:
        release.set()
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await app.close()
