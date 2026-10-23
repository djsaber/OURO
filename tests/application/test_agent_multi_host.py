from __future__ import annotations

import asyncio
import multiprocessing

import pytest

from ouro.application.agent_models import SpawnAgentRequest
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ProcessResult, ToolExecutionContext, ToolRegistry
from ouro.tools.builtin_shell import CommandShell, builtin_command_tool
from tests.application.helpers import wait_for_agent_interactions, wait_for_parent_delivery


class DelegatingProvider:
    provider_id = "test"

    def __init__(self, *, spawn: bool = False, fail_child: bool = False):
        self.spawn = spawn
        self.fail_child = fail_child

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
        primary = any(tool["name"] == "SpawnAgent" for tool in tools or [])
        last_input = max(
            i
            for i, message in enumerate(messages)
            if message.role == "user" or message.text_content.startswith("<subagent-message>")
        )
        results = [message for message in messages[last_input:] if message.role == "tool_result"]
        if primary:
            if self.spawn and not results:
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": "spawn",
                        "name": "SpawnAgent",
                        "arguments": {
                            "agent_id": "general",
                            "task": "Read system information",
                            "description": "Cross-host child",
                            "execution_mode": "sync",
                        },
                    }
                )
            else:
                yield ProviderStreamEvent(type="text_delta", text="Parent settled")
        elif self.fail_child:
            raise RuntimeError("Child provider unavailable")
        elif len(results) < 2:
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": f"shell_{len(results)}",
                    "name": "PowerShell",
                    "arguments": {"command": "Get-Date"},
                }
            )
        else:
            yield ProviderStreamEvent(type="text_delta", text="Child completed")
        yield ProviderStreamEvent(type="done")


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_late_owner_settlement_cannot_rewind_shared_session_activity(
    tmp_path, backend, monkeypatch
):
    first = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    second = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    parked, release = asyncio.Event(), asyncio.Event()
    original = first.runtime.run_prepared

    async def delayed_return(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result.run.status == "waiting_approval":
            parked.set()
            await release.wait()
        return result

    monkeypatch.setattr(first.runtime, "run_prepared", delayed_return)
    async with first, second:
        parent = await first.runtime.run("Prepare a delegation")
        child = await first.agents.spawn_agent(
            SpawnAgentRequest(
                request_id="late-owner",
                parent_session_id=parent.session.session_id,
                parent_run_id=parent.run.run_id,
                agent_id="general",
                task="Read information",
                description="Late owner settlement",
                execution_mode="sync",
            )
        )
        subscription = await first.supervisor.subscribe(
            child.child_session_id, include_activity=True
        )
        try:
            await asyncio.wait_for(parked.wait(), 5)
            pending = await interaction(second, parent.session.session_id)
            handle = await second.agents.resolve_agent_approval(
                pending.delegation_id,
                child_session_id=pending.child_session_id,
                child_run_id=pending.child_run_id,
                tool_call_id=pending.tool_call_id,
                approval_id=pending.request_id,
                approved=False,
            )
            # Deny both requested calls; no shell process is invoked.
            assert (await second.supervisor.wait(handle)).status == "waiting_approval"
            pending = await interaction(second, parent.session.session_id)
            handle = await second.agents.resolve_agent_approval(
                pending.delegation_id,
                child_session_id=pending.child_session_id,
                child_run_id=pending.child_run_id,
                tool_call_id=pending.tool_call_id,
                approval_id=pending.request_id,
                approved=False,
            )
            assert (await second.supervisor.wait(handle)).status == "completed"
            async with asyncio.timeout(5):
                while first.supervisor.activity(child.child_session_id).status != "completed":
                    await asyncio.sleep(0.01)
            owner = first.supervisor._jobs_by_session[child.child_session_id]
            release.set()
            await owner.done.wait()
            assert owner.result.run.status == "waiting_approval"
            assert first.supervisor.activity(child.child_session_id).status == "completed"
        finally:
            release.set()
            subscription.close()


def application(tmp_path, backend, executable, *, kind="powershell", spawn=False, fail_child=False):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    tools = ToolRegistry()
    tools.register(
        builtin_command_tool(
            CommandShell(
                kind=kind,
                executable=executable,
                tool_name="PowerShell",
                display_name="PowerShell 7" if kind == "powershell" else "Windows PowerShell",
            )
        )
    )
    runtime = RuntimeService(
        store=store,
        tools=tools,
        adapter=RuntimeAdapter(
            DelegatingProvider(spawn=spawn, fail_child=fail_child),
            ModelMetadata(model_id="test", provider_id="test"),
        ),
        environment=RuntimeEnvironment.local(
            tmp_path, store.sessions_dir if backend == "files" else None
        ),
    )
    return LocalApplication.from_runtime(runtime)


async def interaction(app, root_id):
    return (await wait_for_agent_interactions(app.agents, root_id))[0]


def _finish_child_in_process(directory, backend, root_id, scenario):
    async def execute():
        app = application(
            directory,
            backend,
            "C:/PowerShell/pwsh.exe",
            kind="windows_powershell" if scenario == "changed" else "powershell",
            spawn=True,
            fail_child=scenario == "provider_failure",
        )
        async with app:
            for index in range(1 if scenario == "provider_failure" else 2):
                pending = await interaction(app, root_id)
                handle = await app.agents.resolve_agent_approval(
                    pending.delegation_id,
                    child_session_id=pending.child_session_id,
                    child_run_id=pending.child_run_id,
                    tool_call_id=pending.tool_call_id,
                    approval_id=pending.request_id,
                    approved=scenario == "changed" and index == 0,
                )
                await app.supervisor.wait(handle)

    asyncio.run(execute())


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("scenario", ["same", "changed", "provider_failure"])
async def test_sync_result_crosses_real_process_boundary(tmp_path, backend, scenario):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe", spawn=True)
    async with app:
        session = app.runtime.sessions.create(
            approval_mode="approval",
            current_model=app.runtime.current_model().key,
        ).session
        root = await app.supervisor.submit("Delegate", session_id=session.session_id)
        waiting = await app.supervisor.wait(root)
        run = app.runtime.store.load_run(root.session_id, waiting.run_id)
        resumed = await app.supervisor.resolve_tool_approval(
            session_id=root.session_id,
            run_id=run.run_id,
            approval_id=run.pending_approval_id,
            approved=True,
        )
        assert (await app.supervisor.wait(resumed)).status == "waiting_child"
        await interaction(app, root.session_id)
        process = multiprocessing.get_context("spawn").Process(
            target=_finish_child_in_process,
            args=(tmp_path, backend, root.session_id, scenario),
        )
        process.start()
        try:
            # Process startup, durable child execution and shutdown are not a
            # 15-second SLA. Keep a deadlock guard and report a live peer clearly.
            await asyncio.to_thread(process.join, 60)
            assert not process.is_alive(), "child Host did not finish its protocol"
            assert process.exitcode == 0
            async with asyncio.timeout(60):
                completed = await app.supervisor.wait_run(root.session_id, run.run_id)
            assert completed.status == "completed"
            messages = app.runtime.store.load_run_messages(root.session_id, run.run_id)
            results = [
                m for m in messages if m.tool_result and m.tool_result.tool_call_id == "spawn"
            ]
            assert len(results) == 1
            assert results[0].tool_result.is_error == (scenario == "provider_failure")
            expected = (
                "Child provider unavailable"
                if scenario == "provider_failure"
                else "Child completed"
            )
            assert expected in results[0].text_content
            assert_child_outcome(app, root.session_id, scenario)
        finally:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 5)
            process.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("scenario", ["same", "changed", "provider_failure"])
async def test_sync_parent_wakes_when_child_finishes_on_another_host(tmp_path, backend, scenario):
    first = application(tmp_path, backend, "C:/PowerShell/pwsh.exe", spawn=True)
    second = application(
        tmp_path,
        backend,
        "C:/PowerShell/pwsh.exe",
        kind="windows_powershell" if scenario == "changed" else "powershell",
        spawn=True,
        fail_child=scenario == "provider_failure",
    )
    async with first, second:
        session = first.runtime.sessions.create(
            approval_mode="approval", current_model=first.runtime.current_model().key
        ).session
        root = await first.supervisor.submit("Delegate", session_id=session.session_id)
        waiting = await first.supervisor.wait(root)
        run = first.runtime.store.load_run(root.session_id, waiting.run_id)
        # The parent and its parked waiter belong to A; only child execution moves to B.
        resumed = await first.supervisor.resolve_tool_approval(
            session_id=root.session_id,
            run_id=run.run_id,
            approval_id=run.pending_approval_id,
            approved=True,
        )
        assert (await first.supervisor.wait(resumed)).status == "waiting_child"
        # Opening the same root on B arms a second durable waiter. Both Hosts
        # must converge on one original ToolCall and one parent continuation.
        await second.agents.reconcile_root(root.session_id)
        for index in range(1 if scenario == "provider_failure" else 2):
            pending = await interaction(second, root.session_id)
            handle = await second.agents.resolve_agent_approval(
                pending.delegation_id,
                child_session_id=pending.child_session_id,
                child_run_id=pending.child_run_id,
                tool_call_id=pending.tool_call_id,
                approval_id=pending.request_id,
                approved=scenario == "changed" and index == 0,
            )
            await second.supervisor.wait(handle)
        # A receipt ends at parking; durable Run observation spans continuation
        # on either Host without synchronous polling that competes with writes.
        await wait_for_parent_delivery(first.supervisor, root.session_id, run.run_id)
        async with asyncio.timeout(60):
            completed = await first.supervisor.wait_run(root.session_id, run.run_id)
        assert completed.status == "completed"
        messages = first.runtime.store.load_runtime_session(root.session_id).messages
        results = [m for m in messages if m.tool_result and m.tool_result.tool_call_id == "spawn"]
        assert len(results) == 1
        assert results[0].tool_result.is_error == (scenario == "provider_failure")
        expected = (
            "Child provider unavailable" if scenario == "provider_failure" else "Child completed"
        )
        assert expected in results[0].text_content
        assert_child_outcome(first, root.session_id, scenario)


def assert_child_outcome(app, root_id, scenario):
    relation = app.runtime.store.list_delegations(root_id)[0]
    child = app.runtime.store.load_run(relation.child_session_id, relation.current_child_run_id)
    assert child.status == ("failed" if scenario == "provider_failure" else "completed")
    if scenario == "changed":
        messages = app.runtime.store.load_run_messages(child.session_id, child.run_id)
        result = next(message for message in messages if message.tool_result)
        assert result.tool_result.is_error
        assert "contract" in result.text_content


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_competing_hosts_resolve_one_approval_once_and_converge(
    tmp_path, backend, monkeypatch
):
    first = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    second = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    entered, release = asyncio.Event(), asyncio.Event()
    executed = []

    async def process(self, arguments, **kwargs):
        executed.append(arguments)
        entered.set()
        await release.wait()
        return ProcessResult(stdout="System information", stderr="", returncode=0)

    monkeypatch.setattr(ToolExecutionContext, "run_process", process)
    async with first, second:
        parent = await first.runtime.run("Prepare a delegation")
        child = await first.agents.spawn_agent(
            SpawnAgentRequest(
                request_id="competing-approval",
                parent_session_id=parent.session.session_id,
                parent_run_id=parent.run.run_id,
                agent_id="general",
                task="Read information",
                description="Concurrent approval",
                execution_mode="sync",
            )
        )
        pending = await interaction(second, parent.session.session_id)
        subscriptions = [
            await app.supervisor.subscribe(child.child_session_id, include_activity=True)
            for app in (first, second)
        ]

        async def resolve(app):
            handle = await app.supervisor.resolve_tool_approval(
                session_id=pending.child_session_id,
                run_id=pending.child_run_id,
                approval_id=pending.request_id,
                approved=True,
            )
            return await app.supervisor.wait(handle)

        attempts = asyncio.gather(resolve(first), resolve(second), return_exceptions=True)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.sleep(0.1)
            release.set()
            results = await asyncio.wait_for(attempts, 5)
            assert len(executed) == 1
            assert (
                sum(getattr(result, "status", None) == "waiting_approval" for result in results)
                == 1
            )
            pending = await interaction(first, parent.session.session_id)
            handle = await first.agents.resolve_agent_approval(
                pending.delegation_id,
                child_session_id=pending.child_session_id,
                child_run_id=pending.child_run_id,
                tool_call_id=pending.tool_call_id,
                approval_id=pending.request_id,
                approved=False,
            )
            assert (await first.supervisor.wait(handle)).status == "completed"
            async with asyncio.timeout(5):
                while any(
                    app.supervisor.activity(child.child_session_id).status != "completed"
                    for app in (first, second)
                ):
                    await asyncio.sleep(0.01)
            assert len(executed) == 1
        finally:
            release.set()
            await attempts
            for subscription in subscriptions:
                subscription.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_child_approvals_and_follow_up_can_move_between_shell_install_paths(
    tmp_path, backend, monkeypatch
):
    executed = []

    async def process(self, arguments, **kwargs):
        executed.append(arguments[0])
        return ProcessResult(stdout="System information", stderr="", returncode=0)

    monkeypatch.setattr(ToolExecutionContext, "run_process", process)
    first_path = "C:/Users/Test/WindowsApps/pwsh.EXE"
    second_path = "C:/Program Files/WindowsApps/PowerShell/pwsh.exe"
    first = application(tmp_path, backend, first_path)
    second = application(tmp_path, backend, second_path)
    async with first, second:
        parent = await first.runtime.run("Prepare a delegation")
        child = await first.agents.spawn_agent(
            SpawnAgentRequest(
                request_id="cross-host",
                parent_session_id=parent.session.session_id,
                parent_run_id=parent.run.run_id,
                agent_id="general",
                task="Read system information",
                description="Cross-host shell",
                execution_mode="sync",
            )
        )
        for resolver, expected in [(second, "waiting_approval"), (first, "completed")]:
            pending = await interaction(resolver, parent.session.session_id)
            handle = await resolver.agents.resolve_agent_approval(
                pending.delegation_id,
                child_session_id=pending.child_session_id,
                child_run_id=pending.child_run_id,
                tool_call_id=pending.tool_call_id,
                approval_id=pending.request_id,
                approved=True,
            )
            result = await asyncio.wait_for(resolver.supervisor.wait(handle), 60)
            assert result.status == expected, [item.message for item in result.diagnostics]
        assert executed == [second_path, first_path]
        relation = first.runtime.store.load_delegation(
            parent.session.session_id, child.delegation_id
        )
        assert relation.result.terminal_status == "completed"
        follow_up = await second.agents.send_agent_message(
            child.delegation_id, message="Read again", mode="follow_up", request_id="again"
        )
        pending = await interaction(first, parent.session.session_id)
        assert pending.child_run_id == follow_up.child_run_id
        assert follow_up.child_run_id != child.child_run_id
        await second.supervisor.abort(child.child_session_id)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_child_starts_with_current_host_capabilities_and_resumes_sync_parent(
    tmp_path, backend
):
    first = application(tmp_path, backend, "C:/PowerShell/pwsh.exe", spawn=True)
    second = application(
        tmp_path, backend, "C:/Windows/powershell.exe", kind="windows_powershell", spawn=True
    )
    async with first, second:
        session = first.runtime.sessions.create(
            approval_mode="approval", current_model=first.runtime.current_model().key
        ).session
        root = await first.supervisor.submit("Delegate", session_id=session.session_id)
        # The accepted task keeps its authority ceiling; the child makes its
        # first decision with the second Host's current implementation.
        waiting = await asyncio.wait_for(first.supervisor.wait(root), 60)
        run = first.runtime.store.load_run(root.session_id, waiting.run_id)
        assert run.status == "waiting_approval"
        resumed = await second.supervisor.resolve_tool_approval(
            session_id=root.session_id,
            run_id=run.run_id,
            approval_id=run.pending_approval_id,
            approved=True,
        )
        assert (
            await asyncio.wait_for(second.supervisor.wait(resumed), 60)
        ).status == "waiting_child"
        for _ in range(2):
            pending = await interaction(second, root.session_id)
            handle = await second.agents.resolve_agent_approval(
                pending.delegation_id,
                child_session_id=pending.child_session_id,
                child_run_id=pending.child_run_id,
                tool_call_id=pending.tool_call_id,
                approval_id=pending.request_id,
                approved=False,
            )
            await second.supervisor.wait(handle)
        await wait_for_parent_delivery(first.supervisor, root.session_id, run.run_id)
        async with asyncio.timeout(60):
            completed = await first.supervisor.wait_run(root.session_id, run.run_id)
        assert completed.status == "completed"
        relation = first.runtime.store.list_delegations(root.session_id)[0]
        child = first.runtime.store.load_run(
            relation.child_session_id, relation.current_child_run_id
        )
        assert first.runtime.store.load_run(root.session_id, run.run_id).status == "completed"
        assert child.status == "completed"
        assert child.error is None
        assert child.ended_at is not None
        assert child.model_round_count == 3
        assert first.runtime.store.load_session_record(child.session_id).active_run_id is None
        assert relation.result.terminal_status == "completed"
        events, _ = first.runtime.store.load_events_after(
            child.session_id, after_sequence=0, limit=100
        )
        assert any(event.event_type == "run_completed" for event in events)
