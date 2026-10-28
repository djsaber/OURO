from __future__ import annotations

import asyncio

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.application.tool_calls import ToolCallCoordinator
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.state import SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v5 as sdk
from ouro.tools import ToolRegistry
from tests.application.tool_runtime import store as store


@pytest.mark.parametrize("conflict", [False, True])
async def test_arrival_persistence_does_not_wait_for_other_business_callbacks(
    store, tmp_path, monkeypatch, conflict
):
    second_staging = asyncio.Event()
    first_saved = asyncio.Event()
    release = asyncio.Event()
    stage = ToolCallCoordinator._stage_control_transitions
    saved = []
    effects = tmp_path / "effects.txt"

    async def callback(call):
        if call.tool_call_id == "first":
            await second_staging.wait()
        with effects.open("a", encoding="utf-8") as stream:
            stream.write(call.tool_call_id + "\n")
        call.session_state.replace(state_schema_version=1, payload={"call": call.tool_call_id})
        return sdk.ToolOutput(content=f"observed {call.tool_call_id}")

    registry = ToolRegistry()
    for index, name in enumerate(("First", "Second")):
        tool = adapt_sdk_contributions(
            sdk.ContributionSet(
                tools=[
                    sdk.ToolContribution(
                        id=name.lower(),
                        exposed_name=name,
                        description="Record an independent external effect.",
                        permission_mode="auto",
                        effect="state_changing",
                        handler=callback,
                    )
                ]
            ),
            owner_id="example.shared" if conflict else f"example.owner{index}",
        ).tools[0]
        registry.register(tool)
    app = RuntimeService(
        store=store,
        tools=registry,
        tool_execution_config=ToolExecutionConfig(mode="parallel", max_concurrency=2),
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            ToolCallRequest(id=name.lower(), name=name, source_index=index)
                        )
                        for index, name in enumerate(("First", "Second"))
                    ]
                    + [ProviderStreamEvent(type="done")],
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    session = SessionRecord()
    store.create_session(session)

    async def held_stage(coordinator, outcome, **kwargs):
        run = store.load_run(session.session_id, outcome.invocation.run_id)
        identity = run.active_tool_batch.identity(run.run_id, outcome.invocation.tool_call_id)
        observed = store.load_tool_outcome(session.session_id, identity, 1)
        assert observed.value.content == f"observed {identity.tool_call_id}"
        saved.append(identity)
        if identity.tool_call_id == "second":
            second_staging.set()
            await release.wait()
        else:
            first_saved.set()
        return await stage(coordinator, outcome, **kwargs)

    monkeypatch.setattr(ToolCallCoordinator, "_stage_control_transitions", held_stage)
    task = asyncio.create_task(app.run("run both", session_id=session.session_id))
    try:
        await asyncio.wait_for(first_saved.wait(), 60)
        assert [identity.tool_call_id for identity in saved] == ["second", "first"]
        assert effects.read_text(encoding="utf-8").splitlines() == ["second", "first"]
        assert not store.load_harness(session.session_id).state.states
        assert not any(
            message.role == "tool_result"
            for message in store.load_session(session.session_id).messages
        )
        release.set()
        result = await asyncio.wait_for(task, 60)
        if conflict:
            assert result.run.status == "failed"
            assert "SessionCommitConflict" in result.run.error
            assert "competing owner state proposals" in result.run.error
            assert not store.load_harness(session.session_id).state.states
            assert not any(
                message.role == "tool_result"
                for message in store.load_session(session.session_id).messages
            )
            await app.run_recovery.reconcile(store.load_runtime_session(session.session_id))
        else:
            assert result.run.status == "completed", result.run.error
            assert {
                state.owner_id for state in store.load_harness(session.session_id).state.states
            } == {"example.owner0", "example.owner1"}
        messages = [
            message
            for message in store.load_session(session.session_id).messages
            if message.role == "tool_result"
        ]
        assert [message.tool_result.tool_call_id for message in messages] == ["first", "second"]
        assert [message.tool_result.is_error for message in messages] == [conflict, conflict]
        for identity in saved:
            outcome = store.load_tool_outcome(session.session_id, identity, 1)
            assert outcome.value.content == f"observed {identity.tool_call_id}"
            assert not outcome.value.is_error
        assert effects.read_text(encoding="utf-8").splitlines() == ["second", "first"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await app.close()
