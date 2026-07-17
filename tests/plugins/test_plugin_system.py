from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from ouro.application.plugins import PluginService
from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.hooks import HookContext, HookRegistry, RunOutcome
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_policies import ToolPolicyCall, ToolPolicyRegistry
from ouro.core.tools import ToolInvocation
from ouro.local_application import build_local_plugin_service, resolve_plugin_credential
from ouro.plugins.compatibility import inspect_community_package
from ouro.plugins.contributions import (
    ContributionCoordinator,
    ContributionOwner,
    PluginContributions,
    PreparedContributionSet,
)
from ouro.plugins.dependencies import PluginDependencyError, resolve_plugin_dependencies
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.package import PluginPackageError, validate_plugin_package
from ouro.plugins.registry import PluginCommand, PluginCommandRegistry
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolExecutionContext, ToolRegistry, ToolResult


def _write_native_plugin(
    root: Path,
    *,
    plugin_id: str = "dev.ouro.echo",
    version: str = "1.0.0",
    runtime: dict[str, object] | None = None,
    contributes: dict[str, object] | None = None,
    dependencies: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    manifest = {
        "schema_version": 1,
        "plugin_id": plugin_id,
        "name": plugin_id.rsplit(".", 1)[-1].title(),
        "version": version,
        "namespace": plugin_id.rsplit(".", 1)[-1].replace("-", "_"),
        "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^1.0.0"},
        "runtime": runtime or {"kind": "declarative"},
        "contributes": contributes or {},
        "permissions": [],
        "dependencies": {"plugins": dependencies or []},
        "metadata": {},
    }
    if extra:
        manifest.update(extra)
    manifest_dir = root / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def _runtime(home: Path):
    tools = ToolRegistry()
    hooks = HookRegistry()
    commands = PluginCommandRegistry()
    compaction = CompactionStrategyRegistry()
    continuations = RunContinuationRegistry()
    tool_policies = ToolPolicyRegistry()
    runtime = PluginRuntimeResource(
        PluginPackageStore(home),
        ContributionCoordinator(
            tools=tools,
            hooks=hooks,
            commands=commands,
            compaction=compaction,
            continuations=continuations,
            tool_policies=tool_policies,
        ),
    )
    return runtime, tools, hooks, commands, compaction


def test_native_manifest_is_strict_and_paths_cannot_escape(tmp_path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    _write_native_plugin(plugin, extra={"official": True})

    with pytest.raises(ValueError, match="official"):
        validate_plugin_package(plugin)

    raw = json.loads((plugin / ".ouro-plugin" / "plugin.json").read_text(encoding="utf-8"))
    raw.pop("official")
    raw["configuration"] = {"schema": "../secret.json", "version": 1}
    (plugin / ".ouro-plugin" / "plugin.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(PluginPackageError, match="must start with"):
        validate_plugin_package(plugin)

    raw["configuration"] = None
    raw["contributes"] = {
        "tools": [
            {"id": "one", "exposed_name": "duplicate", "description": "One."},
            {"id": "two", "exposed_name": "duplicate", "description": "Two."},
        ]
    }
    (plugin / ".ouro-plugin" / "plugin.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate tool exposed_name"):
        validate_plugin_package(plugin)


def test_install_is_immutable_and_never_imports_entrypoint(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "side_effect_plugin:create_plugin"},
    )
    marker = tmp_path / "imported.txt"
    (source / "side_effect_plugin.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )
    service = build_local_plugin_service(tmp_path / "home")

    result = service.install(source)

    assert result.status == "disabled"
    assert not marker.exists()
    detail = service.show("dev.ouro.echo")
    package_path = tmp_path / "home" / "plugins" / "store" / "dev.ouro.echo" / detail.package_digest
    assert package_path.is_dir()
    assert service.install(source).changed is False


@pytest.mark.asyncio
async def test_inprocess_lifecycle_requires_separate_grants_and_drains_snapshot(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "lifecycle_plugin:create_plugin"},
        contributes={
            "tools": [
                {
                    "id": "run",
                    "exposed_name": "echo__run",
                    "description": "Echo input.",
                    "permission_mode": "auto",
                }
            ]
        },
    )
    (source / "lifecycle_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet, ToolContribution, ToolOutput

class Plugin:
    def __init__(self, context):
        self.context = context

    async def prepare(self):
        async def run(call):
            return ToolOutput(content=str(call.arguments.get('value', '')))
        return ContributionSet(tools=[ToolContribution(
            id='run', exposed_name='echo__run', description='Echo input.',
            permission_mode='auto', handler=run,
        )])

    async def start(self):
        self.context.data.write_bytes('status.txt', b'started')

    async def stop(self, reason, deadline):
        self.context.data.write_bytes('status.txt', reason.encode())

def create_plugin(context):
    return Plugin(context)
""",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    runtime, tools, *_ = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)

    assert (await service.enable("dev.ouro.echo")).status == "needs_code_trust"
    service.authorize("dev.ouro.echo")
    assert service.check("dev.ouro.echo").status == "needs_code_trust"
    service.trust_code("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"
    definition = tools.get("echo__run").definition
    assert definition.permission_mode == "approval"
    assert definition.metadata["contract_hash"]
    result = await tools.get("echo__run").execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="echo__run",
            arguments={"value": "ok"},
        )
    )
    assert result.content == "ok"

    snapshot = runtime.snapshot()
    disabling = asyncio.create_task(service.disable("dev.ouro.echo"))
    await asyncio.sleep(0)
    assert not disabling.done()
    snapshot.close()
    assert (await disabling).status == "disabled"
    assert tools.names() == []
    status_path = home / "plugins" / "data" / "dev.ouro.echo" / "status.txt"
    assert status_path.read_text(encoding="utf-8") == "disable"
    await runtime.close()


@pytest.mark.asyncio
async def test_catalog_barrier_reconfigure_and_revoke_are_generation_safe(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "plugin:create_plugin"},
        contributes={
            "tools": [
                {
                    "id": "run",
                    "exposed_name": "safe__run",
                    "description": "Run safely.",
                }
            ]
        },
    )
    (source / "plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet, ToolContribution, ToolOutput
class Plugin:
    async def prepare(self):
        async def run(call):
            return ToolOutput(content='ok')
        return ContributionSet(tools=[ToolContribution(
            id='run', exposed_name='safe__run', description='Run safely.', handler=run,
        )])
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    runtime, tools, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")
    service.trust_code("dev.ouro.echo")

    empty_snapshot = runtime.snapshot()
    enabling = asyncio.create_task(service.enable("dev.ouro.echo"))
    await asyncio.sleep(0)
    assert not enabling.done()
    with pytest.raises(RuntimeError, match="catalog is changing"):
        runtime.snapshot()
    empty_snapshot.close()
    assert (await enabling).status == "active"

    with runtime.snapshot() as snapshot:
        first_generation = snapshot.generations[0].generation_id
    configured = await service.configure("dev.ouro.echo", {"mode": "new"})
    assert configured.status == "active"
    with runtime.snapshot() as snapshot:
        assert snapshot.generations[0].generation_id != first_generation

    active_snapshot = runtime.snapshot()
    revoking = asyncio.create_task(service.revoke("dev.ouro.echo"))
    await asyncio.sleep(0)
    assert tools.names() == []
    assert not revoking.done()
    active_snapshot.close()
    assert (await revoking).status == "needs_authorization"
    await runtime.close()


@pytest.mark.asyncio
async def test_declarative_skill_is_projected_as_owned_context(tmp_path) -> None:
    source = tmp_path / "skill-plugin"
    (source / "skills" / "calendar").mkdir(parents=True)
    _write_native_plugin(
        source,
        contributes={"skills": [{"id": "calendar", "path": "./skills/calendar"}]},
    )
    (source / "skills" / "calendar" / "SKILL.md").write_text(
        "---\nname: calendar\ndescription: Manage a calendar.\n---\n\nInstructions.\n",
        encoding="utf-8",
    )
    runtime, _tools, hooks, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"

    result = await hooks.dispatch(HookContext(hook_name="after_context_build"))
    combined = "\n".join(text for item in result.results for text in item.added_context)
    assert "Manage a calendar" in combined
    assert "SKILL.md" in combined
    await service.disable("dev.ouro.echo")
    assert hooks.registered("after_context_build") == []
    await runtime.close()


@pytest.mark.asyncio
async def test_commands_and_background_services_use_owned_lifecycle(tmp_path) -> None:
    source = tmp_path / "service-plugin"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "service:create_plugin"},
        contributes={
            "commands": [{"id": "hello", "description": "Say hello."}],
            "background_services": [{"id": "watcher"}],
        },
    )
    (source / "service.py").write_text(
        """from ouro.sdk.plugins.v1 import (
    BackgroundServiceContribution, CommandContribution, ContributionSet,
)
class Plugin:
    def __init__(self, context): self.context = context
    async def prepare(self):
        async def command(call): return 'hello ' + call.argument
        async def start_service(): self.context.data.write_bytes('watcher.txt', b'started')
        async def stop_service(): self.context.data.write_bytes('watcher.txt', b'stopped')
        return ContributionSet(
            commands=[CommandContribution(id='hello', description='Say hello.', handler=command)],
            background_services=[BackgroundServiceContribution(
                id='watcher', start=start_service, stop=stop_service,
            )],
        )
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin(context)
""",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    runtime, _tools, _hooks, commands, _compaction = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime, commands=commands)
    service.install(source)
    service.authorize("dev.ouro.echo")
    service.trust_code("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"

    assert service.list_commands()[0].command_id == "dev.ouro.echo:hello"
    result = await service.execute_command("dev.ouro.echo:hello", "world")
    assert result.output == "hello world"
    status = home / "plugins" / "data" / "dev.ouro.echo" / "watcher.txt"
    assert status.read_text(encoding="utf-8") == "started"
    await service.disable("dev.ouro.echo")
    assert status.read_text(encoding="utf-8") == "stopped"
    assert service.list_commands() == []
    await runtime.close()


def test_contribution_commit_rolls_back_every_registry_on_late_conflict(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(
        source,
        contributes={
            "tools": [
                {
                    "id": "run",
                    "exposed_name": "echo__run",
                    "description": "Run.",
                }
            ],
            "commands": [{"id": "go", "description": "Go."}],
        },
    )
    service = build_local_plugin_service(tmp_path / "home")
    installed = service.installer.install(source).installed
    tools = ToolRegistry()
    commands = PluginCommandRegistry()
    commands.register(
        PluginCommand(
            name="go",
            plugin_id="dev.ouro.echo",
            handler=lambda _request: "builtin conflict",
        )
    )
    coordinator = ContributionCoordinator(
        tools=tools,
        hooks=HookRegistry(),
        commands=commands,
    )
    from ouro.plugins.models import PluginGenerationRecord

    generation = PluginGenerationRecord(
        generation_id="plg_test",
        plugin_id=installed.plugin_id,
        version=installed.version,
        package_digest=installed.package_digest,
        runtime_kind="declarative",
    )
    prepared = coordinator.prepare(
        installed,
        generation,
        PluginContributions(
            tools=[
                Tool(
                    definition=ToolDefinition(name="echo__run", description="Run."),
                    handler=lambda invocation: ToolResult(
                        tool_call_id=invocation.tool_call_id,
                        tool_name=invocation.tool_name,
                    ),
                )
            ],
            commands=[PluginCommand(name="go", handler=lambda _request: "go")],
        ),
    )

    with pytest.raises(ValueError, match="already registered"):
        coordinator.commit(prepared)

    assert tools.names() == []
    assert [item.command_id for item in commands.list()] == ["dev.ouro.echo:go"]


def test_failed_owner_replacement_restores_previous_lease_ownership() -> None:
    tools = ToolRegistry()
    coordinator = ContributionCoordinator(tools=tools, hooks=HookRegistry())
    previous_owner = ContributionOwner("dev.ouro.echo", "digest-1", "generation-1")
    previous_lease = coordinator.commit(
        PreparedContributionSet(
            owner=previous_owner,
            tools=(
                Tool(
                    definition=ToolDefinition(name="echo__run", description="Run."),
                    handler=lambda invocation: ToolResult(
                        tool_call_id=invocation.tool_call_id,
                        tool_name=invocation.tool_name,
                    ),
                ),
            ),
        )
    )

    replacement = PreparedContributionSet(
        owner=ContributionOwner("dev.ouro.echo", "digest-2", "generation-2"),
        commands=(PluginCommand(name="go", handler=lambda _request: "go"),),
    )
    with pytest.raises(ValueError, match="command registry is unavailable"):
        coordinator.commit(replacement, replace_owner=previous_owner)

    assert tools.names() == ["echo__run"]
    previous_lease.close()
    assert tools.names() == []


def test_same_version_different_content_is_side_by_side_and_diagnostic(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_native_plugin(first)
    _write_native_plugin(second)
    (first / "content.txt").write_text("one", encoding="utf-8")
    (second / "content.txt").write_text("two", encoding="utf-8")
    service = build_local_plugin_service(tmp_path / "home")

    service.install(first)
    result = service.install(second)

    assert result.diagnostics[0].code == "version_digest_mismatch"
    assert len(service.show("dev.ouro.echo").installs) == 2


def test_archive_traversal_is_rejected_before_extraction(tmp_path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.txt", "bad")

    result = build_local_plugin_service(tmp_path / "home").validate(archive)

    assert result.valid is False
    assert "escapes package root" in result.diagnostics[0].message
    assert not (tmp_path / "escape.txt").exists()


def test_archive_rejects_nonportable_nested_path_before_extraction(tmp_path) -> None:
    archive = tmp_path / "alternate-stream.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("plugin/payload.txt:metadata", "bad")

    result = build_local_plugin_service(tmp_path / "home").validate(archive)

    assert result.valid is False
    assert "not portable" in result.diagnostics[0].message


def test_worker_and_mcp_process_paths_are_statically_audited(tmp_path) -> None:
    worker = tmp_path / "worker-path"
    worker.mkdir()
    _write_native_plugin(
        worker,
        runtime={"kind": "worker", "command": ["python", "./worker.py"]},
    )
    (worker / "worker.py").write_text("", encoding="utf-8")
    with pytest.raises(PluginPackageError, match="start with './'"):
        validate_plugin_package(worker)

    mcp = tmp_path / "mcp-plugin"
    (mcp / "mcp").mkdir(parents=True)
    _write_native_plugin(
        mcp,
        contributes={"mcp_servers": [{"id": "calendar", "path": "./mcp/calendar.json"}]},
        extra={
            "permissions": [
                {
                    "id": "process.spawn",
                    "scopes": ["{python}"],
                    "reason": "Start the packaged MCP server.",
                }
            ]
        },
    )
    (mcp / "server.py").write_text("", encoding="utf-8")
    (mcp / "mcp" / "calendar.json").write_text(
        json.dumps({"command": "{python}", "args": ["./server.py"]}),
        encoding="utf-8",
    )
    package = validate_plugin_package(mcp)
    assert package.manifest.contributes.mcp_servers[0].id == "calendar"

    manifest_path = mcp / ".ouro-plugin" / "plugin.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["permissions"] = []
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PluginPackageError, match="process.spawn"):
        validate_plugin_package(mcp)


@pytest.mark.asyncio
async def test_worker_runtime_uses_stdio_protocol_and_closes_process(tmp_path) -> None:
    source = tmp_path / "worker"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.worker",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "1.0",
        },
        contributes={
            "tools": [
                {
                    "id": "echo",
                    "exposed_name": "worker__echo",
                    "description": "Echo through worker.",
                }
            ]
        },
    )
    (source / "worker.py").write_text(
        """import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    result = {}
    if method == 'initialize':
        result = {'protocol_version': '1.0'}
    elif method == 'contributions.prepare':
        result = {'tools': {'echo': {'parameters_schema': {'type': 'object'}}}}
    elif method == 'tool.call':
        call = request['params']['call']
        result = {'content': call['arguments'].get('value', '') + '|' + str(call['workspace'])}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown':
        break
""",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    runtime, tools, *_ = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.worker")
    service.trust_code("dev.ouro.worker")

    assert (await service.enable("dev.ouro.worker")).status == "active"
    result = await tools.get("worker__echo").execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="worker__echo",
            arguments={"value": "worker-ok"},
        ),
        context=ToolExecutionContext(workspace=tmp_path, workspace_only=True),
    )
    assert result.content == f"worker-ok|{tmp_path}"
    await service.disable("dev.ouro.worker")
    await runtime.close()


@pytest.mark.asyncio
async def test_worker_supports_session_inputs_and_semantic_policies(tmp_path) -> None:
    source = tmp_path / "worker-continuation"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.worker_goal",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "1.0",
        },
        contributes={
            "commands": [{"id": "goal", "description": "Start a goal."}],
            "continuation_policies": [{"id": "goal"}],
            "tool_policies": [{"id": "guard"}],
        },
        extra={
            "permissions": [
                {"id": "sessions.write", "reason": "Write goal input."},
                {"id": "background.run", "reason": "Continue the goal."},
            ]
        },
    )
    (source / "worker.py").write_text(
        """import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    result = {}
    if method == 'initialize':
        result = {'protocol_version': '1.0'}
    elif method == 'contributions.prepare':
        result = {
            'commands': ['goal'],
            'continuation_policies': ['goal'],
            'tool_policies': ['guard'],
        }
    elif method == 'command.call':
        argument = request['params']['call']['argument']
        result = {
            'output': 'started',
            'session_inputs': [{'prompt': 'Goal: ' + argument, 'mode': 'follow_up'}],
        }
    elif method == 'continuation.call':
        result = {'action': 'continue', 'prompt': 'Continue goal', 'state_revision': 3}
    elif method == 'tool_policy.call':
        result = {'action': 'deny', 'reason': 'worker guard denied the call'}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown':
        break
""",
        encoding="utf-8",
    )
    runtime, _, _, commands, _ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime, commands=commands)
    service.install(source)
    service.authorize("dev.ouro.worker_goal")
    service.trust_code("dev.ouro.worker_goal")

    assert (await service.enable("dev.ouro.worker_goal")).status == "active"
    command = await service.execute_command("dev.ouro.worker_goal:goal", "ship")
    assert command.session_inputs[0].prompt == "Goal: ship"
    policy = runtime.coordinator.continuations.registered()[0]
    decision = await policy.handler(
        RunOutcome(
            session_id="ses_worker",
            run_id="run_worker",
            status="completed",
            origin="user",
        )
    )
    assert decision.action == "continue"
    assert decision.prompt == "Continue goal"
    assert decision.state_revision == 3
    tool_policy = runtime.coordinator.tool_policies.registered()[0]
    tool_decision = await tool_policy.handler(
        ToolPolicyCall(
            session_id="ses_worker",
            run_id="run_worker",
            tool_call_id="call_worker",
            tool_name="Bash",
            arguments={"command": "unsafe"},
        )
    )
    assert tool_decision.action == "deny"
    assert tool_decision.reason == "worker guard denied the call"
    await service.disable("dev.ouro.worker_goal")
    await runtime.close()


@pytest.mark.asyncio
async def test_worker_initialize_must_confirm_manifest_protocol(tmp_path) -> None:
    source = tmp_path / "worker-protocol"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.protocol",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "1.0",
        },
    )
    (source / "worker.py").write_text(
        """import json, sys
for line in sys.stdin:
    request = json.loads(line)
    result = {'protocol_version': '1.1'} if request.get('method') == 'initialize' else {}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if request.get('method') == 'shutdown': break
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.trust_code("dev.ouro.protocol")

    assert (await service.enable("dev.ouro.protocol")).status == "failed"
    assert any(
        "protocol negotiation failed" in item.message
        for item in service.diagnostics("dev.ouro.protocol")
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_worker_credentials_use_declared_slots_without_protocol_or_log_leaks(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "worker-credentials"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.credentials",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "1.0",
        },
        extra={
            "permissions": [
                {
                    "id": "credentials.use",
                    "scopes": ["calendar_api"],
                    "reason": "Authenticate calendar requests.",
                }
            ]
        },
    )
    (source / "worker.py").write_text(
        """import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    params = request.get('params', {})
    result = {}
    error = None
    if method == 'initialize':
        if 'credentials' in params:
            error = {'code': -32602, 'message': 'raw credentials in initialize'}
        else:
            name = params['credential_environment']['calendar_api']
            print(os.environ[name], file=sys.stderr, flush=True)
            result = {'protocol_version': '1.0'}
    elif method == 'contributions.prepare':
        result = {}
    if 'id' in request:
        response = {'jsonrpc': '2.0', 'id': request['id']}
        response['error' if error else 'result'] = error or result
        print(json.dumps(response), flush=True)
    if method == 'shutdown':
        break
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OURO_PLUGIN_TEST_SECRET", "calendar-secret-value")
    runtime, *_ = _runtime(tmp_path / "home")
    runtime.credential_resolver = resolve_plugin_credential
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.credentials")
    service.trust_code("dev.ouro.credentials")

    with pytest.raises(PluginPackageError, match="credential slot"):
        await service.configure(
            "dev.ouro.credentials",
            {},
            credential_refs={"undeclared": "env:OURO_PLUGIN_TEST_SECRET"},
        )
    await service.configure(
        "dev.ouro.credentials",
        {},
        credential_refs={"calendar_api": "env:OURO_PLUGIN_TEST_SECRET"},
    )
    assert (await service.enable("dev.ouro.credentials")).status == "active"
    await asyncio.sleep(0.05)
    assert all(
        "calendar-secret-value" not in diagnostic.message
        for diagnostic in service.diagnostics("dev.ouro.credentials")
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_inprocess_resolved_credentials_are_redacted_from_all_plugin_outputs(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    source = tmp_path / "inprocess-credentials"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.inprocess_credentials",
        runtime={"kind": "python-inprocess", "entrypoint": "credential_plugin:create_plugin"},
        extra={
            "permissions": [
                {
                    "id": "credentials.use",
                    "scopes": ["service_api"],
                    "reason": "Authenticate service requests.",
                }
            ]
        },
    )
    (source / "credential_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet
class Plugin:
    def __init__(self, context): self.context = context
    async def prepare(self): return ContributionSet()
    async def start(self):
        secret = self.context.credentials.resolve('service_api')
        self.context.logger.warning('resolved value: %s', secret)
        self.context.diagnostics.emit('credential_test', secret)
        raise RuntimeError(secret)
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin(context)
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OURO_INPROCESS_SECRET", "opaque-inprocess-secret-value")
    runtime, *_ = _runtime(tmp_path / "home")
    runtime.credential_resolver = resolve_plugin_credential
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.inprocess_credentials")
    service.trust_code("dev.ouro.inprocess_credentials")
    await service.configure(
        "dev.ouro.inprocess_credentials",
        {},
        credential_refs={"service_api": "env:OURO_INPROCESS_SECRET"},
    )

    assert (await service.enable("dev.ouro.inprocess_credentials")).status == "failed"
    assert all(
        "opaque-inprocess-secret-value" not in record.getMessage() for record in caplog.records
    )
    assert all(
        "opaque-inprocess-secret-value" not in item.message
        for item in service.diagnostics("dev.ouro.inprocess_credentials")
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_broken_desired_plugin_does_not_block_unrelated_startup(tmp_path) -> None:
    good = tmp_path / "good"
    broken = tmp_path / "broken"
    good.mkdir()
    broken.mkdir()
    _write_native_plugin(good, plugin_id="dev.ouro.good")
    _write_native_plugin(
        broken,
        plugin_id="dev.ouro.broken",
        dependencies=[{"plugin_id": "dev.ouro.missing", "version": "^1.0.0"}],
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(good)
    service.install(broken)

    def enable_all(state):
        state.plugins["dev.ouro.good"].activation.desired_enabled = True
        state.plugins["dev.ouro.broken"].activation.desired_enabled = True
        return state

    service.store.mutate_state(enable_all)
    await runtime.start()

    with runtime.snapshot() as snapshot:
        assert [item.plugin_id for item in snapshot.generations] == ["dev.ouro.good"]
    assert any(
        item.plugin_id == "dev.ouro.broken" and item.code == "plugin_dependency_resolution_failed"
        for item in runtime.diagnostics()
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_corrupted_immutable_package_cannot_activate(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(source)
    (source / "payload.txt").write_text("original", encoding="utf-8")
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    installed = service.install(source)
    package = runtime.store.package_path("dev.ouro.echo", installed.package_digest)
    (package / "payload.txt").write_text("tampered", encoding="utf-8")

    assert (await service.enable("dev.ouro.echo")).status == "failed"
    assert service.check("dev.ouro.echo").status == "invalid"
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_digest_selection_restores_previous_active_generation(tmp_path) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    contributes = {
        "tools": [
            {
                "id": "run",
                "exposed_name": "rollback__run",
                "description": "Run the selected generation.",
            }
        ]
    }
    _write_native_plugin(
        first_source,
        version="1.0.0",
        runtime={"kind": "python-inprocess", "entrypoint": "first_plugin:create_plugin"},
        contributes=contributes,
    )
    _write_native_plugin(
        second_source,
        version="2.0.0",
        runtime={"kind": "python-inprocess", "entrypoint": "second_plugin:create_plugin"},
        contributes=contributes,
    )
    (first_source / "first_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet, ToolContribution
class Plugin:
    async def prepare(self):
        async def run(call): return 'first'
        return ContributionSet(tools=[ToolContribution(
            id='run', exposed_name='rollback__run',
            description='Run the selected generation.', handler=run,
        )])
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    (second_source / "second_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet, ToolContribution
class Plugin:
    async def prepare(self):
        async def run(call): return 'second'
        return ContributionSet(tools=[ToolContribution(
            id='run', exposed_name='rollback__run',
            description='Run the selected generation.', handler=run,
        )])
    async def start(self): raise RuntimeError('candidate failed')
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    runtime, tools, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    first = service.install(first_source)
    second = service.install(second_source)
    for digest in (first.package_digest, second.package_digest):
        service.authorize("dev.ouro.echo", package_digest=digest)
        service.trust_code("dev.ouro.echo", package_digest=digest)
    assert (await service.enable("dev.ouro.echo")).status == "active"

    with pytest.raises(RuntimeError, match="previous generation was restored"):
        await service.select("dev.ouro.echo", second.package_digest)

    assert service.show("dev.ouro.echo").package_digest == first.package_digest
    result = await tools.get("rollback__run").execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="rollback__run",
            arguments={},
        )
    )
    assert result.content == "first"
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_active_configuration_restores_previous_generation(tmp_path) -> None:
    source = tmp_path / "configuration-rollback"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.configuration",
        runtime={"kind": "python-inprocess", "entrypoint": "configured:create_plugin"},
        extra={"configuration": {"schema": "./configuration.schema.json", "version": 1}},
    )
    (source / "configuration.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"fail": {"type": "boolean"}},
                "required": ["fail"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (source / "configured.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet
class Plugin:
    def __init__(self, context): self.fail = context.configuration['fail']
    async def prepare(self): return ContributionSet()
    async def start(self):
        if self.fail: raise RuntimeError('new configuration failed')
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin(context)
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.trust_code("dev.ouro.configuration")
    await service.configure("dev.ouro.configuration", {"fail": False})
    assert (await service.enable("dev.ouro.configuration")).status == "active"

    with pytest.raises(RuntimeError, match="previous generation was restored"):
        await service.configure("dev.ouro.configuration", {"fail": True})

    record = service.store.read_state().plugins["dev.ouro.configuration"]
    selected = record.selected()
    assert selected is not None
    assert record.activation.configuration_for(selected.package_digest).values == {"fail": False}
    assert service.show("dev.ouro.configuration").status == "active"
    await runtime.close()


@pytest.mark.asyncio
async def test_sdk_tool_id_must_match_manifest_even_when_exposed_name_matches(tmp_path) -> None:
    source = tmp_path / "tool-identity"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.tool_identity",
        runtime={"kind": "python-inprocess", "entrypoint": "identity:create_plugin"},
        contributes={
            "tools": [
                {
                    "id": "declared",
                    "exposed_name": "identity__run",
                    "description": "Run.",
                }
            ]
        },
    )
    (source / "identity.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet, ToolContribution
class Plugin:
    async def prepare(self):
        async def run(call): return 'bad'
        return ContributionSet(tools=[ToolContribution(
            id='different', exposed_name='identity__run', description='Run.', handler=run,
        )])
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.tool_identity")
    service.trust_code("dev.ouro.tool_identity")

    assert (await service.enable("dev.ouro.tool_identity")).status == "failed"
    assert any(
        "tool contribution id does not match manifest" in item.message
        for item in service.diagnostics("dev.ouro.tool_identity")
    )
    assert service.show("dev.ouro.tool_identity").status == "failed"
    await runtime.close()


@pytest.mark.asyncio
async def test_dependency_disable_and_incompatible_selection_are_rejected_atomically(
    tmp_path,
) -> None:
    dependency_v1 = tmp_path / "dependency-v1"
    dependency_v2 = tmp_path / "dependency-v2"
    dependant = tmp_path / "dependant"
    dependency_v1.mkdir()
    dependency_v2.mkdir()
    dependant.mkdir()
    _write_native_plugin(
        dependency_v1,
        plugin_id="dev.ouro.dependency",
        version="1.0.0",
    )
    _write_native_plugin(
        dependency_v2,
        plugin_id="dev.ouro.dependency",
        version="2.0.0",
    )
    _write_native_plugin(
        dependant,
        plugin_id="dev.ouro.dependant",
        dependencies=[{"plugin_id": "dev.ouro.dependency", "version": "^1.0.0"}],
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    first = service.install(dependency_v1)
    second = service.install(dependency_v2)
    service.install(dependant)

    assert (await service.enable("dev.ouro.dependant")).status == "active"
    with pytest.raises(PluginDependencyError, match="active dependents"):
        await service.disable("dev.ouro.dependency")
    with pytest.raises(PluginDependencyError, match="does not satisfy"):
        await service.select("dev.ouro.dependency", second.package_digest)
    assert service.show("dev.ouro.dependency").package_digest == first.package_digest

    await service.disable("dev.ouro.dependant")
    await service.disable("dev.ouro.dependency")
    await runtime.close()


@pytest.mark.asyncio
async def test_durable_generation_references_block_replacement_and_enable_is_idempotent(
    tmp_path,
) -> None:
    first_source = tmp_path / "durable-first"
    second_source = tmp_path / "durable-second"
    first_source.mkdir()
    second_source.mkdir()
    _write_native_plugin(first_source, version="1.0.0")
    _write_native_plugin(second_source, version="2.0.0")
    home = tmp_path / "home"
    sessions = LocalSessionStore(home)
    sessions.initialize()
    runtime, *_ = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime, session_store=sessions)
    first = service.install(first_source)
    second = service.install(second_source)
    assert (await service.enable("dev.ouro.echo")).status == "active"
    with runtime.snapshot() as snapshot:
        generation = snapshot.generations[0]

    # Repeating enable is a state assertion, not an implicit restart.
    assert (await service.enable("dev.ouro.echo")).status == "active"
    with runtime.snapshot() as snapshot:
        assert snapshot.generations[0].generation_id == generation.generation_id

    session = SessionRecord(status="waiting_approval")
    sessions.create_session(session)
    run = RunRecord(
        session_id=session.session_id,
        status="waiting_approval",
        metadata={"extension_generations": [generation.model_dump(mode="json")]},
    )
    sessions.save_run(run)

    with pytest.raises(ValueError, match="active durable runs"):
        await service.restart("dev.ouro.echo")
    with pytest.raises(ValueError, match="active durable runs"):
        await service.disable("dev.ouro.echo")
    with pytest.raises(ValueError, match="active durable runs"):
        await service.select("dev.ouro.echo", second.package_digest)
    with pytest.raises(ValueError, match="active durable runs"):
        await service.configure("dev.ouro.echo", {})
    assert service.show("dev.ouro.echo").package_digest == first.package_digest

    sessions.save_run(run.model_copy(update={"status": "completed"}))
    assert (await service.restart("dev.ouro.echo")).status == "active"
    await service.disable("dev.ouro.echo")
    await runtime.close()


@pytest.mark.asyncio
async def test_remove_scans_runs_without_loading_session_journals(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(source)
    home = tmp_path / "home"
    sessions = LocalSessionStore(home)
    sessions.initialize()
    runtime, *_ = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime, session_store=sessions)
    service.install(source)

    session = SessionRecord(title="Broken journal")
    sessions.create_session(session)
    # Durable-reference checks read Run records only; a broken message journal
    # in an unrelated Session must not block plugin lifecycle operations.
    journal = sessions.sessions_dir / session.session_id / "messages.jsonl"
    journal.write_text("{not json\n", encoding="utf-8")

    result = await service.remove("dev.ouro.echo")

    assert result.status == "disabled"
    await runtime.close()


@pytest.mark.asyncio
async def test_shared_data_survives_nonfinal_digest_removal(tmp_path) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first_source.mkdir()
    second_source.mkdir()
    _write_native_plugin(first_source, version="1.0.0")
    _write_native_plugin(second_source, version="2.0.0")
    home = tmp_path / "home"
    runtime, *_ = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime)
    service.install(first_source)
    second = service.install(second_source)
    data = home / "plugins" / "data" / "dev.ouro.echo" / "state.txt"
    data.parent.mkdir(parents=True)
    data.write_text("keep", encoding="utf-8")
    assert (await service.enable("dev.ouro.echo")).status == "active"

    with pytest.raises(ValueError, match="final installed digest"):
        await service.remove(
            "dev.ouro.echo",
            package_digest=second.package_digest,
            purge_data=True,
        )
    result = await service.remove(
        "dev.ouro.echo",
        package_digest=second.package_digest,
    )
    assert result.status == "active"
    assert data.read_text(encoding="utf-8") == "keep"

    await service.disable("dev.ouro.echo")
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_shutdown_drains_generation_snapshots(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(source)
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    assert (await service.enable("dev.ouro.echo")).status == "active"

    snapshot = runtime.snapshot()
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    assert not closing.done()
    snapshot.close()
    await closing


def test_dependency_cycle_and_community_incompatibility_are_explicit(tmp_path) -> None:
    home = tmp_path / "home"
    service = build_local_plugin_service(home)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_native_plugin(
        first,
        plugin_id="dev.ouro.first",
        dependencies=[{"plugin_id": "dev.ouro.second", "version": "^1.0.0"}],
    )
    _write_native_plugin(
        second,
        plugin_id="dev.ouro.second",
        dependencies=[{"plugin_id": "dev.ouro.first", "version": "^1.0.0"}],
    )
    service.install(first)
    service.install(second)
    state = service.store.read_state()
    state.plugins["dev.ouro.first"].activation.desired_enabled = True
    state.plugins["dev.ouro.second"].activation.desired_enabled = True

    with pytest.raises(PluginDependencyError, match="cycle"):
        resolve_plugin_dependencies(state)

    community = tmp_path / "community"
    community.mkdir()
    (community / "package.json").write_text(
        json.dumps({"version": "1.0.0", "keywords": ["opencode"], "hooks": ["before"]}),
        encoding="utf-8",
    )
    report = inspect_community_package(community)
    assert report.source_format == "opencode"
    assert any(item.outcome == "incompatible" for item in report.items)


def test_community_report_discovers_root_mcp_descriptor(tmp_path) -> None:
    community = tmp_path / "community-mcp"
    community.mkdir()
    (community / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "calendar": {"command": "calendar-server", "args": []},
                    "remote": {"type": "http", "url": "https://example.test/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )

    report = inspect_community_package(community)

    assert {item.identity for item in report.items} == {"calendar", "remote"}
    assert report.required_permissions == ["network.outbound", "process.spawn"]


def test_native_manifest_model_rejects_unknown_security_fields() -> None:
    with pytest.raises(ValueError, match="verified"):
        PluginManifest.model_validate(
            {
                "schema_version": 1,
                "plugin_id": "dev.ouro.strict",
                "name": "Strict",
                "version": "1.0.0",
                "namespace": "strict",
                "engines": {"ouro": "^0.1.0", "plugin_api": "^1.0.0"},
                "verified": True,
            }
        )
