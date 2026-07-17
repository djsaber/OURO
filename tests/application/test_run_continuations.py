from __future__ import annotations

import asyncio
import json
import threading

import pytest

from ouro.application.plugin_models import PluginCommandView
from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.run_continuations import RunContinuationService
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.cli.interactive import run_interactive
from ouro.cli.interactive_commands import handle_command
from ouro.core.adapter import RuntimeAdapter
from ouro.core.continuations import (
    ContinuationDecision,
    ContinuationPolicyRegistration,
    RunContinuationRegistry,
)
from ouro.core.hooks import HookRegistration, HookRegistry, HookResult
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.session_controls import SessionInputRequest
from ouro.core.state import (
    RunContinuationCheckpoint,
    RunContinuationControllerRef,
    RunRecord,
    SessionRecord,
)
from ouro.local_application import LocalApplication
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginCommandResult,
)
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


class CountingProvider:
    provider_id = "continuation-test"

    def __init__(self) -> None:
        self.calls = 0
        self.contexts = []

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, tools
        self.calls += 1
        self.contexts.append(list(messages))
        yield ProviderStreamEvent(type="text_delta", text=f"answer-{self.calls}")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=True,
        )


class GatedProvider(CountingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    async def stream(self, *, model, system_prompt="", messages, tools=None):
        del model, system_prompt, tools
        self.calls += 1
        self.contexts.append(list(messages))
        if self.calls == 1:
            self.started.set()
            await asyncio.to_thread(self.release.wait)
        yield ProviderStreamEvent(type="text_delta", text=f"answer-{self.calls}")
        yield ProviderStreamEvent(type="done")


def _write_goal_plugin(source) -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "plugin_id": "dev.ouro.goal",
                "name": "Goal",
                "version": "1.0.0",
                "namespace": "goal",
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^1.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": "goal_plugin:create_plugin",
                },
                "contributes": {
                    "commands": [{"id": "goal", "description": "Start goal mode."}],
                    "continuation_policies": [{"id": "goal", "priority": 10, "timeout_seconds": 2}],
                },
                "permissions": [
                    {
                        "id": "sessions.write",
                        "reason": "Persist attributed goal continuation inputs.",
                    },
                    {
                        "id": "background.run",
                        "reason": "Continue an active goal after a Run completes.",
                    },
                ],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (source / "goal_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import (
    CommandContribution,
    CommandOutput,
    ContinuationDecision,
    ContinuationPolicyContribution,
    ContributionSet,
    SessionInput,
)

class Plugin:
    async def prepare(self):
        async def goal(call):
            return CommandOutput(
                output='Goal started.',
                session_inputs=(SessionInput(prompt='Work on: ' + call.argument),),
            )

        async def continue_goal(outcome):
            if outcome.origin == 'user':
                return ContinuationDecision(
                    action='continue',
                    prompt='Check the goal and continue if it is not complete.',
                    reason='goal_still_active',
                    state_revision=1,
                )
            return ContinuationDecision(
                action='complete',
                reason='goal_complete',
                state_revision=2,
            )

        return ContributionSet(
            commands=[CommandContribution(
                id='goal', description='Start goal mode.', handler=goal,
            )],
            continuation_policies=[ContinuationPolicyContribution(
                id='goal', handler=continue_goal,
            )],
        )

    async def start(self): pass
    async def stop(self, reason, deadline): pass

def create_plugin(context):
    return Plugin()
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_plugin_continuation_creates_attributed_synthetic_follow_up(
    tmp_path,
    backend,
) -> None:
    home = tmp_path / "home"
    store = (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.sqlite3")
    )
    store.initialize()
    provider = CountingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="continuation", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "goal-plugin"
    source.mkdir()
    _write_goal_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    application.plugins.authorize("dev.ouro.goal")
    application.plugins.trust_code("dev.ouro.goal")
    await application.start()
    assert (await application.plugins.enable("dev.ouro.goal")).status == "active"

    result = await runtime.run("Solve the task")

    snapshot = store.load_runtime_session(result.session.session_id)
    assert provider.calls == 2
    assert [run.origin for run in snapshot.runs] == ["user", "plugin"]
    assert [run.continuation.action for run in snapshot.runs if run.continuation] == [
        "continue",
        "complete",
    ]
    assert snapshot.runs[1].parent_run_id == snapshot.runs[0].run_id
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "system_synthetic",
        "assistant",
    ]
    synthetic = snapshot.messages[2]
    assert synthetic.metadata["plugin_id"] == "dev.ouro.goal"
    assert synthetic.metadata["contribution_id"] == "goal"
    assert synthetic.metadata["continuation_decision_id"]
    assert synthetic.text_content.startswith("Check the goal")
    events, _ = store.load_events_page(result.session.session_id, limit=100)
    assert any(event.event_type == "run_continuation_resolved" for event in events)

    await application.close()


@pytest.mark.asyncio
async def test_plugin_command_returns_permission_checked_session_input(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    provider = CountingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="command", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "goal-plugin"
    source.mkdir()
    _write_goal_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    application.plugins.authorize("dev.ouro.goal")
    application.plugins.trust_code("dev.ouro.goal")
    await application.start()
    assert (await application.plugins.enable("dev.ouro.goal")).status == "active"

    alias = await handle_command(
        "/goal inspect the feature",
        runtime,
        None,
        plugins=application.plugins,
    )
    assert alias.output == "Goal started."
    assert alias.session_inputs[0].prompt == "Work on: inspect the feature"

    command = await handle_command(
        "/dev.ouro.goal:goal ship the feature",
        runtime,
        None,
        plugins=application.plugins,
    )
    assert command.output == "Goal started."
    assert len(command.session_inputs) == 1
    attributed = command.session_inputs[0]
    assert attributed.plugin_id == "dev.ouro.goal"
    assert attributed.contribution_id == "goal"
    assert attributed.generation_id

    result = await runtime.submit_plugin_input(attributed, session_id=None)
    snapshot = store.load_runtime_session(result.session.session_id)
    assert len(snapshot.runs) == 1
    assert snapshot.runs[0].origin == "plugin"
    assert snapshot.messages[0].role == "system_synthetic"
    assert snapshot.messages[0].text_content == "Work on: ship the feature"

    await application.close()


@pytest.mark.asyncio
async def test_interactive_plugin_run_accepts_commands_while_model_is_running(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    provider = GatedProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="interactive", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "goal-plugin"
    source.mkdir()
    _write_goal_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    application.plugins.authorize("dev.ouro.goal")
    application.plugins.trust_code("dev.ouro.goal")
    await application.start()
    assert (await application.plugins.enable("dev.ouro.goal")).status == "active"
    commands = iter(["/goal target", "/goal guide", "/exit"])

    def input_func(_prompt: str) -> str:
        command = next(commands)
        if command == "/goal guide":
            assert provider.started.wait(timeout=2)
        if command == "/exit":
            provider.release.set()
        return command

    code = await run_interactive(
        runtime=runtime,
        plugins=application.plugins,
        input_func=input_func,
        output_func=lambda _text: None,
    )

    assert code == 0
    session = store.list_sessions()[0]
    events, _ = store.load_events_page(session.session_id, limit=100)
    assert any(event.event_type == "follow_up_input_queued" for event in events)
    await application.close()


@pytest.mark.asyncio
async def test_after_run_hook_receives_typed_terminal_outcome(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CountingProvider()
    seen = []

    def observe(call):
        seen.append(call.run_outcome)
        return HookResult(diagnostics=["observed"])

    hooks = HookRegistry()
    hooks.register(
        HookRegistration(
            plugin_id="dev.ouro.observer",
            hook_id="after",
            hook_name="after_run",
            handler=observe,
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="hook", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
        hooks=hooks,
    )

    result = await runtime.run("Observe this")

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0].run_id == result.run.run_id
    assert seen[0].status == "completed"
    assert seen[0].final_text == "answer-1"
    await runtime.close()


@pytest.mark.asyncio
async def test_continuation_conflict_pauses_without_choosing_by_priority(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controllers = [
        _controller("dev.ouro.first", "goal", "gen_first"),
        _controller("dev.ouro.second", "goal", "gen_second"),
    ]
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=controllers),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "first",
        (_policy("dev.ouro.first", "gen_first", "first scaffold", priority=1),),
    )
    registry.replace_owner(
        "second",
        (_policy("dev.ouro.second", "gen_second", "second scaffold", priority=2),),
    )
    service = RunContinuationService(
        store=store,
        registry=registry,
        queue=QueuedInputQueue(store),
    )

    settlement = await service.settle(session_id=session.session_id, run_id=run.run_id)

    assert settlement.run.continuation is not None
    assert settlement.run.continuation.action == "pause"
    assert settlement.run.continuation.reason == "continuation_policy_conflict"
    assert not QueuedInputQueue(store).eligible(store.load_runtime_session(session.session_id))


@pytest.mark.asyncio
async def test_repeated_continuation_settlement_enqueues_once(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controller = _controller("dev.ouro.goal", "goal", "gen_goal")
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "goal",
        (_policy("dev.ouro.goal", "gen_goal", "continue once"),),
    )
    queue = QueuedInputQueue(store)
    service = RunContinuationService(store=store, registry=registry, queue=queue)

    first = await service.settle(session_id=session.session_id, run_id=run.run_id)
    second = await service.settle(session_id=session.session_id, run_id=run.run_id)

    queued = queue.eligible(store.load_runtime_session(session.session_id))
    assert first.settled is True
    assert first.queued is True
    assert second.settled is False
    assert len(queued) == 1
    assert queued[0].queue_id == first.run.continuation.queue_id


@pytest.mark.asyncio
async def test_continuation_matches_rebuilt_generation_by_execution_contract(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controller = _controller("dev.ouro.goal", "goal", "gen_before_restart")
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "goal-after-restart",
        (_policy("dev.ouro.goal", "gen_after_restart", "continue after restart"),),
    )
    queue = QueuedInputQueue(store)

    settlement = await RunContinuationService(
        store=store,
        registry=registry,
        queue=queue,
    ).settle(session_id=session.session_id, run_id=run.run_id)

    queued = queue.eligible(store.load_runtime_session(session.session_id))
    assert settlement.queued is True
    assert len(queued) == 1
    assert queued[0].generation_id == "gen_after_restart"
    assert queued[0].execution_contract_hash == controller.execution_contract_hash


@pytest.mark.asyncio
async def test_queued_user_input_supersedes_stale_automatic_continuation(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    controller = _controller("dev.ouro.goal", "goal", "gen_goal")
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(controllers=[controller]),
    )
    store.save_run(run)
    registry = RunContinuationRegistry()
    registry.replace_owner(
        "goal",
        (_policy("dev.ouro.goal", "gen_goal", "stale automatic scaffold"),),
    )
    queue = QueuedInputQueue(store)
    queue.enqueue(
        snapshot=store.load_runtime_session(session.session_id),
        prompt="Use this newer user guidance",
        title=None,
    )
    service = RunContinuationService(store=store, registry=registry, queue=queue)

    settlement = await service.settle(session_id=session.session_id, run_id=run.run_id)

    queued = queue.eligible(store.load_runtime_session(session.session_id))
    assert settlement.queued is False
    assert settlement.run.continuation is not None
    assert settlement.run.continuation.action == "pause"
    assert settlement.run.continuation.reason == "superseded_by_queued_input"
    assert [(item.source, item.prompt) for item in queued] == [
        ("user", "Use this newer user guidance")
    ]


@pytest.mark.asyncio
async def test_command_session_input_requires_explicit_permissions() -> None:
    registry = PluginCommandRegistry()
    registry.register(
        PluginCommand(
            plugin_id="dev.ouro.unprivileged",
            name="goal",
            generation_id="gen_unprivileged",
            handler=lambda request: PluginCommandResult(
                command_id=request.command_id,
                session_inputs=[SessionInputRequest(prompt="not authorized")],
            ),
        )
    )

    with pytest.raises(PluginCommandError, match="sessions.write and background.run"):
        await registry.execute("dev.ouro.unprivileged:goal")


@pytest.mark.asyncio
async def test_plugin_command_short_alias_reports_ambiguity(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CountingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="alias", provider_id=provider.provider_id),
        ),
        tools=ToolRegistry(),
    )

    class Commands:
        def list_commands(self):
            return [
                PluginCommandView(
                    command_id="dev.ouro.first:goal",
                    plugin_id="dev.ouro.first",
                    name="goal",
                ),
                PluginCommandView(
                    command_id="dev.ouro.second:goal",
                    plugin_id="dev.ouro.second",
                    name="goal",
                ),
            ]

    result = await handle_command("/goal target", runtime, None, plugins=Commands())

    assert result.output == (
        "Ambiguous plugin command /goal; use one of: /dev.ouro.first:goal, /dev.ouro.second:goal"
    )
    await runtime.close()


def _controller(
    plugin_id: str,
    policy_id: str,
    generation_id: str,
) -> RunContinuationControllerRef:
    return RunContinuationControllerRef(
        plugin_id=plugin_id,
        policy_id=policy_id,
        version="1.0.0",
        package_digest=f"digest-{plugin_id}",
        generation_id=generation_id,
        contract_hash=f"contract-{plugin_id}",
        execution_contract_hash=f"execution-{plugin_id}",
    )


def _policy(
    plugin_id: str,
    generation_id: str,
    prompt: str,
    *,
    priority: int = 100,
) -> ContinuationPolicyRegistration:
    def decide(_outcome):
        return ContinuationDecision(action="continue", prompt=prompt)

    return ContinuationPolicyRegistration(
        plugin_id=plugin_id,
        policy_id="goal",
        handler=decide,
        priority=priority,
        generation_id=generation_id,
        execution_contract_hash=f"execution-{plugin_id}",
        contract_hash=f"contract-{plugin_id}",
    )
