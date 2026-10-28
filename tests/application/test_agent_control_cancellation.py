from __future__ import annotations

import asyncio

import pytest

from ouro.application.contracts.agents import SpawnAgentRequest
from ouro.core.provider import ProviderStreamEvent
from tests.application.test_agent_call_identity import ReusedCallProvider
from tests.application.test_agent_multi_host import application


class SteerProvider(ReusedCallProvider):
    delegation_id = None

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
        if any(tool["name"] == "SpawnAgent" for tool in tools or []):
            if self.delegation_id is None:
                yield ProviderStreamEvent(type="text_delta", text="Parent prepared")
            else:
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": "steer",
                        "name": "SendAgentMessage",
                        "arguments": {
                            "delegation_id": self.delegation_id,
                            "message": "One accepted direction",
                            "mode": "steer",
                        },
                    }
                )
            yield ProviderStreamEvent(type="done")
            return
        async for event in super().stream(
            model=model,
            messages=messages,
            system_prompt=system_prompt,
            tools=tools,
            request_context=request_context,
        ):
            yield event


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("cancellation", ["abort", "task"])
@pytest.mark.parametrize("returns_result", [False, True])
@pytest.mark.parametrize("execution_mode", ["sync", "async"])
async def test_spawn_control_owns_cancellation_and_retains_actual_observations(
    tmp_path, backend, cancellation, returns_result, execution_mode, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(
        tmp_path / "external-children.txt", delegations=1, execution_mode=execution_mode
    )
    app.runtime.adapter.provider = provider
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = app.agents.execute_control
    calls = []

    async def hold_return(request, **kwargs):
        control = await execute(request, **kwargs)
        calls.append((kwargs["session"].session_id, kwargs["identity"], control))
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            if not returns_result:
                raise
            return control

    monkeypatch.setattr(app.agents, "execute_control", hold_return)
    async with app:
        execution = asyncio.create_task(app.runtime.run("Dispatch before cancellation"))
        abort = None
        try:
            await asyncio.wait_for(entered.wait(), 60)
            await asyncio.wait_for(provider.entered[0].wait(), 60)
            session_id, identity, control = calls[0]
            store = app.runtime.store
            start = store.load_tool_call_start(session_id, identity, 1)
            assert start is not None
            assert store.load_tool_outcome(session_id, identity, 1) is None
            if cancellation == "abort":
                abort = asyncio.create_task(app.runtime.abort_run(session_id=session_id))
            else:
                execution.cancel()
            await asyncio.wait_for(cancelled.wait(), 60)
            assert not execution.done()
            assert store.load_tool_outcome(session_id, identity, 1) is None
            release.set()
            provider.release[0].set()
            if cancellation == "task":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(execution, 60)
            else:
                assert (await asyncio.wait_for(execution, 60)).run.status == "aborted"
                await asyncio.wait_for(abort, 60)
            outcome = store.load_tool_outcome(session_id, identity, 1)
            if execution_mode == "sync":
                relation = store.list_delegations(session_id)[0]
                await app.supervisor.wait_run(
                    relation.child_session_id, relation.current_child_run_id
                )
                await app.agents._finalize_result(relation.delegation_id)
                outcome = store.load_tool_outcome(session_id, identity, 1)
                assert outcome is not None
                assert outcome.value.content
                assert outcome.value.structured["result"]["terminal_status"] in {
                    "completed",
                    "aborted",
                }
                assert (
                    outcome.value.structured["handle"]["child_run_id"]
                    == relation.current_child_run_id
                )
            elif returns_result:
                assert outcome is not None
                assert outcome.value.content == control.content
                assert outcome.value.structured == control.structured
                assert outcome.value.started_at is not None
                assert outcome.value.ended_at is not None
                assert not outcome.value.is_error
            else:
                assert outcome is None
            assert store.load_tool_call_start(session_id, identity, 1) == start
            assert all(
                message.role != "tool_result"
                for message in store.load_run_messages(session_id, identity.run_id)
            )
            assert provider.effects.read_text(encoding="utf-8").splitlines() == ["child 0"]
        finally:
            release.set()
            for child_release in provider.release:
                child_release.set()
            execution.cancel()
            await asyncio.gather(
                execution, *([abort] if abort is not None else []), return_exceptions=True
            )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_cancelled_send_retains_return_and_one_accepted_child_input(
    tmp_path, backend, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = SteerProvider(tmp_path / "external-children.txt")
    app.runtime.adapter.provider = provider
    accepted, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = app.agents.execute_control
    observations = []

    async def hold_send(request, **kwargs):
        result = await execute(request, **kwargs)
        observations.append((kwargs["identity"], result))
        accepted.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return result

    async with app:
        seed = await app.runtime.run("Prepare a child to receive directions")
        child = await app.agents.spawn_agent(
            SpawnAgentRequest(
                request_id="host/steer-target",
                parent_session_id=seed.session.session_id,
                parent_run_id=seed.run.run_id,
                agent_id="general",
                task="Work until directed",
                description="Steer target",
            )
        )
        await asyncio.wait_for(provider.entered[0].wait(), 60)
        provider.delegation_id = child.delegation_id
        monkeypatch.setattr(app.agents, "execute_control", hold_send)
        execution = asyncio.create_task(
            app.runtime.run("Send one direction", session_id=seed.session.session_id)
        )
        abort = None
        try:
            await asyncio.wait_for(accepted.wait(), 60)
            store = app.runtime.store
            pending = store.load_session_record(child.child_session_id).pending_input_ids
            assert len(pending) == 1
            input_id = pending[0]
            input_fact = store.load_input(child.child_session_id, input_id)
            assert "One accepted direction" in input_fact.item.prompt
            identity, returned = observations[0]
            abort = asyncio.create_task(app.runtime.abort_run(session_id=seed.session.session_id))
            await asyncio.wait_for(cancelled.wait(), 60)
            assert not execution.done()
            release.set()
            assert (await asyncio.wait_for(execution, 60)).run.status == "aborted"
            await asyncio.wait_for(abort, 60)
            outcome = store.load_tool_outcome(seed.session.session_id, identity, 1)
            assert outcome is not None
            assert outcome.value.content == returned.content
            assert outcome.value.structured == returned.structured
            assert store.load_input(child.child_session_id, input_id) == input_fact
            assert store.load_session_record(child.child_session_id).pending_input_ids == pending
        finally:
            release.set()
            for child_release in provider.release:
                child_release.set()
            execution.cancel()
            await asyncio.gather(
                execution, *([abort] if abort is not None else []), return_exceptions=True
            )
