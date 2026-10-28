from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ouro.application.harness import capture_harness
from ouro.application.runs.tool_batches import load_tool_batch
from ouro.core.errors import PersistenceError
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolInvocation
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.worker.instance import WorkerPluginInstance
from ouro.sdk.plugins import v5 as sdk
from ouro.sessions.backends.files.commits import FileSessionCommits
from ouro.tools import ToolExecutionContext, ToolExecutor
from tests.application.helpers import running_runtime_task
from tests.application.tool_runtime import runtime
from tests.application.tool_runtime import store as store
from tests.harness_state import replace_harness_state
from tests.run_facts import run_tool_results
from tests.tool_batches import persist_tool_batch, resolved_tool_batch


@pytest.fixture(params=["inprocess", "worker"])
async def state_tool(tmp_path, request):
    if request.param == "inprocess":

        def handler(call):
            call.session_state.replace(state_schema_version=1, payload=dict(call.arguments))
            return sdk.ToolOutput(content="updated")

        owner = "example.state"
        tool = adapt_sdk_contributions(
            sdk.ContributionSet(
                tools=[
                    sdk.ToolContribution(
                        id="state",
                        exposed_name="State",
                        description="Store a value.",
                        permission_mode="auto",
                        effect="state_changing",
                        handler=handler,
                    )
                ]
            ),
            owner_id=owner,
        ).tools[0]
        arguments = {"value": "verify"}
        yield tool, owner, arguments, arguments
        return
    package = Path(__file__).parents[2] / "examples" / "plugins" / "worker-tool"
    raw = json.loads((package / ".ouro-plugin" / "plugin.json").read_text(encoding="utf-8"))
    raw["contributes"]["tools"][0]["permission_mode"] = "auto"
    manifest = PluginManifest.model_validate(raw)
    worker = WorkerPluginInstance(
        manifest,
        package_root=package,
        package_digest="test",
        permissions=frozenset(),
        configuration={},
        data_directory=tmp_path / "worker",
        credentials={},
        diagnostic=lambda *_args: None,
    )
    try:
        contributions = await worker.prepare()
        tool = adapt_sdk_contributions(contributions, owner_id=manifest.plugin_id).tools[0]
        yield tool, manifest.plugin_id, {"value": "verify"}, {"calls": 1, "last_value": "verify"}
    finally:
        await worker.stop("shutdown", 0)


async def test_state_and_tool_message_commit_together_and_undo_restores_before_state(
    store,
    state_tool,
    monkeypatch,
):
    tool, owner, arguments, expected = state_tool
    app = runtime(store, tool, arguments)
    native_commit = store.commit_session
    result_commits = []

    def observe(request):
        steps = [step for step in request.steps if step.message and step.message.tool_result]
        if steps:
            assert not store.load_harness(request.before.session_id).state.states
            assert len(steps) == 1
            assert steps[0].harness.states[0].payload == expected
            result_commits.append(request)
        return native_commit(request)

    monkeypatch.setattr(store, "commit_session", observe)
    result = await app.run("track this work")
    assert result.run.status == "completed"
    assert len(result_commits) == 1
    snapshot = store.load_session(result.session.session_id)
    state = store.load_harness(result.session.session_id).state.states[0]
    assert (state.owner_id, state.payload) == (owner, expected)
    tool_message = next(message for message in snapshot.messages if message.tool_result)
    before = store.load_harness(result.session.session_id, tool_message.before_harness_version)
    assert not before.state.states
    assert snapshot.messages[-1].before_harness_version == snapshot.session.harness_version
    store.commit_session(
        SessionCommit(
            before=snapshot.session,
            before_message_id=snapshot.messages[0].message_id,
        )
    )
    assert not store.load_harness(snapshot.session.session_id).state.states
    assert not store.load_session(snapshot.session.session_id).messages


@pytest.mark.parametrize("failure", ["exception", "invalid_result"])
async def test_failed_callback_discards_staged_state(store, failure):
    def handler(call):
        call.session_state.replace(state_schema_version=1, payload={"unaccepted": True})
        if failure == "exception":
            raise ValueError("callback failed")
        return object()

    tool = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="state",
                    exposed_name="State",
                    description="stage a change",
                    permission_mode="auto",
                    effect="state_changing",
                    handler=handler,
                )
            ]
        ),
        owner_id="example.plugin",
    ).tools[0]
    app = runtime(store, tool, {})
    result = await app.run("try")
    assert run_tool_results(store, result)[0].is_error
    assert not store.load_harness(result.session.session_id).state.states


@pytest.mark.parametrize("interaction_mode", ["interactive", "unattended"])
async def test_waiting_state_commits_with_request_and_resume_gets_a_fresh_view(
    store, interaction_mode
):
    def request(call):
        call.session_state.replace(state_schema_version=1, payload={"stage": "waiting"})
        return sdk.ToolInputRequest(data={"question": "Accept?"})

    def resume(call):
        assert call.session_state.read().payload == {"stage": "waiting"}
        call.session_state.replace(state_schema_version=1, payload={"stage": "accepted"})
        return sdk.ToolOutput(content="accepted")

    tool = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="wait",
                    exposed_name="Wait",
                    description="wait with state",
                    permission_mode="auto",
                    effect="state_changing",
                    batch_behavior="exclusive",
                    handler=request,
                    resume_handler=resume,
                    supports_unattended=True,
                )
            ]
        ),
        owner_id="example.wait",
    ).tools[0]
    app = runtime(store, tool, {})
    session = SessionRecord(interaction_mode=interaction_mode)
    store.create_session(session)
    pending = await app.run("confirm", session_id=session.session_id)
    session_id = pending.session.session_id
    if interaction_mode == "interactive":
        assert pending.run.status == "waiting_user"
        snapshot = store.load_session(session_id)
        state = store.load_harness(session_id).state
        assert state.states[0].payload == {"stage": "waiting"}
        run = store.load_run(session_id, pending.run.run_id)
        assert load_tool_batch(store, run).calls[0].input_request.harness_change is None
        assert all(message.role != "tool_result" for message in snapshot.messages)
        result = await app.answer_user(session_id=session_id, run_id=run.run_id, answer="yes")
    else:
        result = pending
    assert result.run.status == "completed"
    assert store.load_harness(session_id).state.states[0].payload == {"stage": "accepted"}
    message = next(item for item in store.load_session(session_id).messages if item.tool_result)
    before = store.load_harness(session_id, message.before_harness_version)
    assert before.state.states[0].payload == {"stage": "waiting"}


async def test_waiting_request_without_state_changes_still_checks_its_captured_version(store):
    entered, release = asyncio.Event(), asyncio.Event()

    async def request(call):
        assert call.session_state.read() is None
        entered.set()
        await release.wait()
        return sdk.ToolInputRequest(data={"question": "based on the old mode"})

    tool = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="wait",
                    exposed_name="Wait",
                    description="wait",
                    handler=request,
                    effect="state_changing",
                    permission_mode="auto",
                    batch_behavior="exclusive",
                )
            ]
        ),
        owner_id="example.wait",
    ).tools[0]
    app = runtime(store, tool, {})
    session = SessionRecord()
    store.create_session(session)
    async with running_runtime_task(app, app.run("wait", session_id=session.session_id)) as task:
        try:
            await entered.wait()
            replace_harness_state(
                store,
                SessionExtensionStateRecord(
                    session_id=session.session_id,
                    owner_id="example.wait",
                    state_schema_version=1,
                    payload="changed concurrently",
                ),
            )
        finally:
            release.set()
        result = await task
        assert result.run.status == "failed"
        assert store.load_session_record(session.session_id).status != "waiting_user"
        assert (
            store.load_harness(session.session_id).state.states[0].payload == "changed concurrently"
        )


@pytest.mark.parametrize(
    ("store", "failure_point"),
    [("files", "response"), ("sqlite", "response"), ("files", "file_application")],
    indirect=["store"],
)
async def test_tool_result_retry_after_durable_decision_is_not_applied_twice(
    store,
    state_tool,
    monkeypatch,
    failure_point,
):
    tool, _owner, arguments, expected = state_tool
    app = runtime(store, tool, arguments)
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    invocation = ToolInvocation(
        session_id=session.session_id,
        run_id=run.run_id,
        tool_call_id="call_state",
        tool_name=tool.definition.name,
        arguments=arguments,
    )
    outcome = await ToolExecutor(app.tools).execute_prepared(
        invocation,
        context=ToolExecutionContext(harness=capture_harness(store, session.session_id)),
    )
    assert outcome.result is not None and not outcome.result.is_error
    batch = resolved_tool_batch(run, [outcome.result])
    batch.calls[0].request.arguments = arguments
    batch.calls[0].invocation = invocation.model_copy(update={"turn_id": batch.turn_id})
    run = persist_tool_batch(store, run, batch)
    commit = store.commit_session

    def lose_response(request):
        commit(request)
        raise OSError("response lost after commit")

    write_suffix = FileSessionCommits._write_suffix

    def interrupt_application(path, offset, records):
        write_suffix(path, offset, records)
        raise OSError("interrupted before publishing the Session state")

    with monkeypatch.context() as fault:
        if failure_point == "response":
            fault.setattr(store, "commit_session", lose_response)
        else:
            fault.setattr(FileSessionCommits, "_write_suffix", staticmethod(interrupt_application))
        with pytest.raises(OSError if failure_point == "response" else PersistenceError):
            app.run_lifecycle.publish_tool_batch(
                session=session,
                run=run,
                batch=batch,
            )
    _, _, _, messages, _ = app.run_lifecycle.publish_tool_batch(
        session=session,
        run=run,
        batch=batch,
    )
    (message,) = messages
    snapshot = store.load_session(session.session_id)
    assert snapshot.messages == [message]
    assert store.load_harness(session.session_id).state.states[0].payload == expected
    store.commit_session(
        SessionCommit(before=snapshot.session, before_message_id=message.message_id)
    )
    with pytest.raises(SessionCommitConflict, match="withdrawn"):
        app.run_lifecycle.publish_tool_batch(
            session=session,
            run=run,
            batch=batch,
        )
    assert not store.load_session(session.session_id).messages
