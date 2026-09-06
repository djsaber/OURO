from __future__ import annotations

import asyncio

import pytest

from ouro.core.errors import PersistenceError
from ouro.core.provider import ProviderStreamEvent
from tests.application.test_agent_multi_host import application, interaction


class ResultProvider:
    provider_id = "test"

    def __init__(self, *, failed=False):
        self.failed = failed
        self.parent_inputs = []

    async def stream(self, *, model, messages, system_prompt="", tools=None):
        primary = any(tool["name"] == "SpawnAgent" for tool in tools or [])
        if primary:
            self.parent_inputs.append(tuple(message.text_content for message in messages))
            if not any(message.role == "tool_result" for message in messages):
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": "spawn",
                        "name": "SpawnAgent",
                        "arguments": {
                            "agent_id": "general",
                            "task": "Read information",
                            "description": "Async feedback",
                            "execution_mode": "async",
                        },
                    }
                )
            else:
                yield ProviderStreamEvent(type="text_delta", text="Parent settled")
        elif not any(message.role == "tool_result" for message in messages):
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "shell",
                    "name": "PowerShell",
                    "arguments": {"command": "Get-Date"},
                }
            )
        elif self.failed:
            raise RuntimeError("child diagnostic evidence")
        else:
            yield ProviderStreamEvent(type="text_delta", text="Child completed")
        yield ProviderStreamEvent(type="done")


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("fault", ["before_accept", "after_accept", "after_schedule"])
async def test_async_cross_host_result_retries_without_duplicate_parent_input(
    tmp_path,
    backend,
    failed,
    fault,
    monkeypatch,
):
    first = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    second = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    providers = [ResultProvider(failed=failed), ResultProvider(failed=failed)]
    first.runtime.adapter.provider = providers[0]
    second.runtime.adapter.provider = providers[1]
    async with first, second:
        session = first.runtime.sessions.create(
            approval_mode="approval",
            current_model=first.runtime.current_model().key,
        ).session
        handle = await first.supervisor.submit("Delegate", session_id=session.session_id)
        waiting = await first.supervisor.wait(handle)
        run = first.runtime.store.load_run(session.session_id, waiting.run_id)
        root = await first.supervisor.resolve_tool_approval(
            session_id=session.session_id,
            run_id=run.run_id,
            approval_id=run.pending_approval_id,
            approved=True,
        )
        assert (await first.supervisor.wait(root)).status == "completed"
        attempts = []
        fault_seen = asyncio.Event()
        for app in (first, second):
            original = app.supervisor.route_host_input

            async def route(request, *, session_id, original=original, app=app):
                if request.semantic_tag != "subagent-result":
                    return await original(request, session_id=session_id)
                attempts.append(request.queue_id)
                if not fault_seen.is_set():
                    fault_seen.set()
                    if fault == "after_accept":
                        await app.runtime.submit_host_input(request, session_id=session_id)
                    elif fault == "after_schedule":
                        await original(request, session_id=session_id)
                    raise OSError("delivery transport temporarily unavailable")
                return await original(request, session_id=session_id)

            monkeypatch.setattr(app.supervisor, "route_host_input", route)
        pending = await interaction(second, session.session_id)
        child = await second.agents.resolve_agent_approval(
            pending.delegation_id,
            child_session_id=pending.child_session_id,
            child_run_id=pending.child_run_id,
            tool_call_id=pending.tool_call_id,
            approval_id=pending.request_id,
            approved=False,
        )
        assert (await second.supervisor.wait(child)).status == ("failed" if failed else "completed")
        async with asyncio.timeout(8):
            while True:
                relation = first.runtime.store.load_delegation(
                    session.session_id, pending.delegation_id
                )
                runs = first.runtime.store.load_runs(session.session_id)
                if (
                    relation.result is not None
                    and relation.result.delivery.status == "delivered"
                    and len(runs) == 2
                    and runs[-1].status == "completed"
                ):
                    break
                await asyncio.sleep(0.01)
        assert fault_seen.is_set()
        assert len(attempts) >= 2
        assert len(set(attempts)) == 1
        assert relation.result.delivery.error is None
        messages = first.runtime.store.load_run_messages(session.session_id, runs[-1].run_id)
        feedback = [message for message in messages if message.semantic_tag == "subagent-result"]
        assert len(feedback) == 1
        evidence = "child diagnostic evidence" if failed else "Child completed"
        assert evidence in feedback[0].text_content
        assert any(
            evidence in content
            for provider in providers
            for inputs in provider.parent_inputs
            for content in inputs
        )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_exhausted_delivery_stays_pending_and_targeted_recovery_retries(
    tmp_path,
    backend,
    monkeypatch,
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    app.runtime.adapter.provider = ResultProvider()
    async with app:
        session = app.runtime.sessions.create(
            approval_mode="auto",
            current_model=app.runtime.current_model().key,
        ).session
        # Fail the provider after its first shell request; deny all real shell execution.
        from ouro.tools import ToolExecutionContext, ToolProcessResult

        async def process(self, arguments, **kwargs):
            return ToolProcessResult(stdout="test", stderr="", returncode=0)

        monkeypatch.setattr(ToolExecutionContext, "run_process", process)
        original = app.supervisor.route_host_input
        attempts = []

        async def unavailable(request, *, session_id):
            attempts.append(request.queue_id)
            raise PersistenceError("store temporarily offline")

        monkeypatch.setattr(app.supervisor, "route_host_input", unavailable)
        handle = await app.supervisor.submit("Delegate", session_id=session.session_id)
        await app.supervisor.wait(handle)
        async with asyncio.timeout(8):
            while len(attempts) < 4 or app.agents._execution._tasks:
                await asyncio.sleep(0.02)
        relation = app.runtime.store.list_delegations(session.session_id)[0]
        assert relation.result.terminal_status == "completed"
        assert relation.result.delivery.status == "pending"
        assert "offline" in relation.result.delivery.error
        assert len(attempts) == 4
        monkeypatch.setattr(app.supervisor, "route_host_input", original)
        await app.agents.reconcile_root(session.session_id)
        relation = app.runtime.store.load_delegation(session.session_id, relation.delegation_id)
        assert relation.result.delivery.status == "delivered"
        assert relation.result.delivery.error is None
