from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from ouro.application.plugins import PluginOperationNotAllowedError, PluginService
from ouro.core.base import utc_now
from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.context_providers import ContextProviderCall
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.interactions import UnattendedResolution
from ouro.core.observers import (
    ContextPreparedObservation,
    ObserverRegistry,
    RunFinishedObservation,
    RunOutcomeView,
    RuntimeEventObservation,
    RuntimeEventView,
    ToolCallFinishedObservation,
    ToolCallRequestedObservation,
)
from ouro.core.prompt_policies import PromptPolicyCall
from ouro.core.session_controls import SessionInputBatchRequest, SessionInputRequest
from ouro.core.state import RunOutcome, RunRecord, SessionRecord
from ouro.core.tool_policies import ToolPolicyCall, ToolPolicyRegistry
from ouro.core.tools import ToolInvocation
from ouro.core.turn_policies import TurnCompletionCandidate
from ouro.local_application import build_local_plugin_service, resolve_plugin_credential
from ouro.plugins.builtin import (
    EMPTY_BUILTIN_PLUGIN_REGISTRY,
    BuiltinPluginDefinition,
    BuiltinPluginRegistry,
)
from ouro.plugins.candidates import PluginRuntimeCandidate
from ouro.plugins.catalog import (
    ExtensionCatalogBase,
    GenerationGate,
    PluginGenerationUnavailableError,
    build_extension_catalog,
)
from ouro.plugins.compatibility import inspect_community_package
from ouro.plugins.contribution_types import contribution_type_adapters
from ouro.plugins.contributions import (
    ContributionCoordinator,
    ContributionOwner,
    PluginContributions,
    PreparedContributionSet,
)
from ouro.plugins.dependencies import (
    PluginDependencyError,
    resolve_plugin_dependencies,
)
from ouro.plugins.manifest import PluginContributes, PluginManifest
from ouro.plugins.mcp import PluginMCPResource
from ouro.plugins.models import (
    InstalledPlugin,
    PluginAuditEvent,
    PluginGenerationRecord,
    PluginStateDocument,
    create_configuration_revision,
)
from ouro.plugins.package import PluginPackageError, validate_plugin_package
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginCommandResult,
)
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import (
    PluginPackageStore,
    PluginStateFormatError,
    PluginStoreError,
    PluginStoreIOError,
)
from ouro.plugins.worker import JsonRpcWorkerClient, WorkerProtocolError
from ouro.sdk.plugins.v1 import ContributionSet
from ouro.sessions import LocalSessionStore
from ouro.skills import SkillMetadata
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolInputRequest,
    ToolRegistry,
    ToolResult,
)

MCP_SERVER_SCRIPT = Path(__file__).parents[1] / "mcp" / "fixtures" / "echo_server.py"


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
        "schema_version": 3,
        "plugin_id": plugin_id,
        "name": plugin_id.rsplit(".", 1)[-1].title(),
        "version": version,
        "namespace": plugin_id.rsplit(".", 1)[-1].replace("-", "_"),
        "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
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


def _runtime(
    home: Path,
    *,
    builtin_plugins: BuiltinPluginRegistry = EMPTY_BUILTIN_PLUGIN_REGISTRY,
    base_skills: tuple[SkillMetadata, ...] = (),
):
    tools = ToolRegistry()
    observers = ObserverRegistry()
    commands = PluginCommandRegistry()
    compaction = CompactionStrategyRegistry()
    continuations = RunContinuationRegistry()
    tool_policies = ToolPolicyRegistry()
    runtime = PluginRuntimeResource(
        PluginPackageStore(home),
        ContributionCoordinator(
            tools=tools,
            observers=observers,
            commands=commands,
            compaction=compaction,
            continuations=continuations,
            tool_policies=tool_policies,
        ),
        builtin_plugins=builtin_plugins,
        base_skills=base_skills,
    )
    return runtime, tools, observers, commands, compaction


class _EmptyBuiltinPlugin:
    async def prepare(self) -> ContributionSet:
        return ContributionSet()

    async def start(self) -> None:
        pass

    async def stop(self, reason: str, deadline: float) -> None:
        del reason, deadline


def _builtin_plugin(
    root: Path,
    *,
    plugin_id: str,
    version: str = "1.0.0",
    dependencies: list[dict[str, object]] | None = None,
    contributes: dict[str, object] | None = None,
    default_enabled: bool = False,
) -> BuiltinPluginDefinition:
    root.mkdir(parents=True, exist_ok=True)
    namespace = plugin_id.rsplit(".", 1)[-1].replace("-", "_")
    return BuiltinPluginDefinition(
        manifest=PluginManifest.model_validate(
            {
                "schema_version": 3,
                "plugin_id": plugin_id,
                "name": plugin_id.rsplit(".", 1)[-1].title(),
                "version": version,
                "namespace": namespace,
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": f"{namespace}_plugin:create_plugin",
                },
                "contributes": contributes or {},
                "permissions": [],
                "dependencies": {"plugins": dependencies or []},
                "metadata": {},
            }
        ),
        factory=lambda _context: _EmptyBuiltinPlugin(),
        root=root,
        default_enabled=default_enabled,
    )


def _user_candidate(installed: InstalledPlugin, root: Path) -> PluginRuntimeCandidate:
    return PluginRuntimeCandidate(
        plugin_class="user",
        manifest=installed.manifest,
        release_id=installed.package_digest,
        root=root,
        configuration=create_configuration_revision(),
        source_kind=installed.provenance.source_kind,
        public_source=installed.provenance.public_source,
        package_digest=installed.package_digest,
    )


def _process_is_running(process_id: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return f'"{process_id}"' in result.stdout
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_test_process(process_id: int) -> None:
    if not _process_is_running(process_id):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            capture_output=True,
            timeout=2,
            check=False,
        )
        return
    try:
        os.kill(process_id, 9)
    except ProcessLookupError:
        pass


class _CatalogGeneration:
    def __init__(
        self,
        record: PluginGenerationRecord,
        contributions: PreparedContributionSet,
    ) -> None:
        self.record = record
        self.contributions = contributions
        self.gate = GenerationGate(record.generation_id)

    def acquire(self):
        return _CatalogGenerationLease(self.record)


class _CatalogGenerationLease:
    def __init__(self, record: PluginGenerationRecord) -> None:
        self.record = record

    def close(self) -> None:
        return None


def test_native_manifest_ignores_unknown_fields_and_paths_cannot_escape(tmp_path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    _write_native_plugin(plugin, extra={"official": True})

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


def test_contribution_type_inventory_is_closed_complete_and_lifecycle_explicit() -> None:
    adapters = contribution_type_adapters()
    expected = set(PluginContributes().by_kind())

    assert {adapter.kind for adapter in adapters} == expected
    assert len(adapters) == len(expected)
    assert {
        adapter.kind for adapter in adapters if adapter.lifecycle == "activation-singleton"
    } == {"background_services"}
    assert all(adapter.projection is not None for adapter in adapters)
    assert all(adapter.preparation is not None for adapter in adapters)

    resources = {adapter.kind for adapter in adapters if adapter.resource is not None}
    assert resources == {"skills", "mcp_servers"}
    for adapter in adapters:
        if adapter.kind in resources:
            assert adapter.sdk is None
            assert adapter.worker is None
        else:
            assert adapter.sdk is not None
            assert adapter.worker is not None
            assert adapter.contract is not None


def test_host_tool_effect_and_batch_behavior_participate_in_catalog_contract_hash() -> None:
    def contract_hash(*, effect: str, batch_behavior: str) -> str:
        tools = ToolRegistry()
        tools.register(
            Tool(
                definition=ToolDefinition(
                    name="WorkflowControl",
                    description="Control a workflow.",
                    effect=effect,
                    batch_behavior=batch_behavior,
                ),
                handler=lambda _invocation: ToolResult(
                    tool_call_id="unused",
                    tool_name="WorkflowControl",
                ),
            )
        )
        base = ExtensionCatalogBase.capture(
            tools=tools,
            observers=ObserverRegistry(),
            commands=PluginCommandRegistry(),
            compaction=CompactionStrategyRegistry(),
            continuations=RunContinuationRegistry(),
            tool_policies=ToolPolicyRegistry(),
        )
        catalog = build_extension_catalog(base, ())
        return catalog.owners["ouro.host/tools/WorkflowControl"].contract_hash

    compatible_read = contract_hash(effect="read_only", batch_behavior="compatible")
    compatible_write = contract_hash(effect="state_changing", batch_behavior="compatible")
    exclusive_read = contract_hash(effect="read_only", batch_behavior="exclusive")

    assert compatible_read != compatible_write
    assert compatible_read != exclusive_read


def test_development_schema_versions_remain_strict_and_removed_fields_are_ignored(
    tmp_path,
) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    _write_native_plugin(plugin)
    manifest_path = plugin / ".ouro-plugin" / "plugin.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))

    raw["schema_version"] = 1
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        validate_plugin_package(plugin)

    raw["schema_version"] = 2
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        validate_plugin_package(plugin)

    raw["schema_version"] = 3
    raw["dependencies"] = {
        "plugins": [
            {
                "plugin_id": "dev.ouro.dependency",
                "version": "^1.0.0",
                "optional_feature": "legacy",
            }
        ]
    }
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    manifest = PluginManifest.model_validate(raw)
    assert "optional_feature" not in manifest.dependencies.plugins[0].model_dump()

    raw["dependencies"] = {"plugins": []}
    raw["contributes"] = {
        "mcp_servers": [
            {
                "id": "legacy",
                "path": "./legacy.json",
                "auto_start": False,
            }
        ]
    }
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    manifest = PluginManifest.model_validate(raw)
    assert "auto_start" not in manifest.contributes.mcp_servers[0].model_dump()

    store = PluginPackageStore(tmp_path / "home")
    store.initialize()
    store.state_path.write_text(
        json.dumps({"schema_version": 1, "plugins": {}}),
        encoding="utf-8",
    )
    with pytest.raises(PluginStoreError, match="invalid plugin state"):
        store.read_state()


def test_prompt_policy_manifest_requires_closed_inspection_permission(tmp_path) -> None:
    plugin = tmp_path / "prompt-policy"
    plugin.mkdir()
    _write_native_plugin(
        plugin,
        contributes={"prompt_policies": [{"id": "guard"}]},
    )
    with pytest.raises(ValueError, match="sessions.prompt.inspect"):
        validate_plugin_package(plugin)

    manifest_path = plugin / ".ouro-plugin" / "plugin.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["permissions"] = [
        {
            "id": "sessions.prompt.inspect",
            "reason": "Inspect prompts before persistence.",
        }
    ]
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    manifest = validate_plugin_package(plugin).manifest
    assert manifest.contributes.prompt_policies[0].id == "guard"


def test_boot_recovery_quarantines_unreadable_state_instead_of_raising(tmp_path) -> None:
    store = PluginPackageStore(tmp_path / "home")
    store.initialize()
    original = json.dumps({"schema_version": 1, "plugins": {}})
    store.state_path.write_text(original, encoding="utf-8")

    recovery = store.recover_state()

    assert recovery.state == PluginStateDocument()
    assert recovery.error is not None
    assert recovery.quarantine_path is not None
    assert recovery.quarantine_path.read_text(encoding="utf-8") == original
    # Recovery heals the document: strict reads see an empty registry again.
    assert not store.state_path.exists()
    assert store.read_state() == PluginStateDocument()


def test_plugin_audit_preserves_unicode_line_separators(tmp_path) -> None:
    store = PluginPackageStore(tmp_path / "home")
    detail = "first\u2028second\u2029third"
    event = PluginAuditEvent(action="test", detail=detail)

    store.append_audit(event)

    assert store.audit_events() == [event]


def test_boot_recovery_passes_through_a_readable_state(tmp_path) -> None:
    store = PluginPackageStore(tmp_path / "home")
    store.initialize()

    recovery = store.recover_state()

    assert recovery.state == PluginStateDocument()
    assert recovery.error is None
    assert recovery.quarantine_path is None


def test_boot_recovery_does_not_quarantine_io_failures(tmp_path, monkeypatch) -> None:
    store = PluginPackageStore(tmp_path / "home")
    store.initialize()
    store.state_path.write_text(PluginStateDocument().model_dump_json(), encoding="utf-8")

    def fail_read(*_args, **_kwargs):
        raise PermissionError("state is temporarily unavailable")

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(PluginStoreIOError, match="could not read plugin state"):
        store.recover_state()

    assert store.state_path.exists()
    assert not list(store.root.glob("state.invalid-*.json"))


def test_state_format_error_remains_a_typed_persistence_failure(tmp_path) -> None:
    store = PluginPackageStore(tmp_path / "home")
    store.initialize()
    store.state_path.write_text('{"schema_version": 1, "plugins": {}}', encoding="utf-8")

    with pytest.raises(PluginStateFormatError, match="invalid plugin state"):
        store.read_state()


def test_plugin_state_schema_two_migrates_without_losing_user_installs(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(source)
    service = build_local_plugin_service(tmp_path / "home")
    installed = service.install(source)
    raw = json.loads(service.store.state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 2
    raw.pop("builtin_activation_overrides")
    service.store.state_path.write_text(json.dumps(raw), encoding="utf-8")

    migrated = service.store.read_state()

    selected = migrated.plugins["dev.ouro.echo"].selected()
    assert selected is not None
    assert selected.package_digest == installed.package_digest
    assert migrated.builtin_activation_overrides == {}
    service.store.mutate_state(lambda state: state)
    persisted = json.loads(service.store.state_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 3


def test_permission_registry_and_builtin_identity_are_enforced_before_install(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(
        source,
        extra={
            "permissions": [
                {
                    "id": "host.unbounded",
                    "reason": "Request an undefined Host capability.",
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="unknown plugin permission id"):
        validate_plugin_package(source)

    _write_native_plugin(source := tmp_path / "reserved")
    store = PluginPackageStore(tmp_path / "home")
    builtin_plugins = BuiltinPluginRegistry(
        (
            _builtin_plugin(
                tmp_path / "builtin-echo",
                plugin_id="dev.ouro.echo",
                contributes={
                    "tools": [
                        {
                            "id": "reserved",
                            "exposed_name": "ReservedBuiltinTool",
                            "description": "Reserved by a builtin plugin.",
                        }
                    ]
                },
            ),
        )
    )
    service = PluginService(
        store,
        builtin_plugins=builtin_plugins,
    )
    with pytest.raises(ValueError, match="cannot replace builtin identity"):
        service.install(source)
    assert not store.package_path("dev.ouro.echo", "unused").parent.exists()

    collision = tmp_path / "collision"
    collision.mkdir()
    _write_native_plugin(
        collision,
        plugin_id="dev.ouro.collision",
        runtime={
            "kind": "python-inprocess",
            "entrypoint": "collision_plugin:create_plugin",
        },
        contributes={
            "tools": [
                {
                    "id": "reserved",
                    "exposed_name": "ReservedBuiltinTool",
                    "description": "Attempts to replace a builtin tool.",
                }
            ]
        },
    )
    (collision / "collision_plugin.py").write_text("def create_plugin(context): ...\n")
    with pytest.raises(ValueError, match="cannot replace builtin tool names"):
        service.install(collision)


def test_builtin_contract_hash_covers_implementation_content(tmp_path) -> None:
    root = tmp_path / "builtin"
    root.mkdir()
    implementation = root / "example_plugin.py"
    implementation.write_text("VALUE = 1\n", encoding="utf-8")
    first = _builtin_plugin(root, plugin_id="dev.ouro.example")

    implementation.write_text("VALUE = 2\n", encoding="utf-8")
    second = _builtin_plugin(root, plugin_id="dev.ouro.example")

    assert first.contract_hash != second.contract_hash


@pytest.mark.asyncio
async def test_configuration_is_content_addressed_and_schema_less_values_are_rejected(
    tmp_path,
) -> None:
    schema_less = tmp_path / "schema-less"
    schema_less.mkdir()
    _write_native_plugin(schema_less)
    service = build_local_plugin_service(tmp_path / "home")
    installed = service.install(schema_less)
    record = service.store.read_state().plugins["dev.ouro.echo"]
    revision = record.activation.configuration_for(installed.package_digest)
    assert revision is not None
    assert revision.values == {}
    assert record.activation.active_configuration_revision_id == revision.revision_id

    with pytest.raises(PluginPackageError, match="requires an empty configuration"):
        await service.configure("dev.ouro.echo", {"undeclared": True})

    raw_state = json.loads(service.store.state_path.read_text(encoding="utf-8"))
    revisions = raw_state["plugins"]["dev.ouro.echo"]["activation"]["configuration_revisions"]
    revisions[revision.revision_id]["values"] = {"tampered": True}
    service.store.state_path.write_text(json.dumps(raw_state), encoding="utf-8")
    with pytest.raises(PluginStoreError, match="revision id does not match"):
        service.store.read_state()


@pytest.mark.asyncio
async def test_plugin_mcp_starts_on_enable_without_manifest_auto_start(tmp_path) -> None:
    source = tmp_path / "mcp"
    (source / "mcp").mkdir(parents=True)
    _write_native_plugin(
        source,
        contributes={"mcp_servers": [{"id": "calendar", "path": "./mcp/calendar.json"}]},
        extra={
            "permissions": [
                {
                    "id": "process.spawn",
                    "reason": "Start the packaged MCP server.",
                }
            ]
        },
    )
    (source / "server.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    (source / "mcp" / "calendar.json").write_text(
        json.dumps({"command": "{python}", "args": ["./server.py"]}),
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")

    assert service.check("dev.ouro.echo").status == "disabled"
    assert service.show("dev.ouro.echo").trust.code_trust == "missing"
    assert (await service.enable("dev.ouro.echo")).status == "needs_code_trust"
    service.trust_code("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "failed"
    assert service.check("dev.ouro.echo").status != "needs_code_trust"
    await runtime.close()


@pytest.mark.asyncio
async def test_optional_plugin_mcp_failure_degrades_without_fake_contract(tmp_path) -> None:
    source = tmp_path / "optional-mcp"
    (source / "mcp").mkdir(parents=True)
    _write_native_plugin(
        source,
        contributes={
            "mcp_servers": [{"id": "calendar", "path": "./mcp/calendar.json", "optional": True}]
        },
        extra={
            "permissions": [{"id": "process.spawn", "reason": "Start the optional MCP server."}]
        },
    )
    (source / "server.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    (source / "mcp" / "calendar.json").write_text(
        json.dumps(
            {
                "command": "{python}",
                "args": ["./server.py"],
                "max_reconnect_attempts": 0,
            }
        ),
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")
    service.trust_code("dev.ouro.echo")

    assert (await service.enable("dev.ouro.echo")).status == "degraded"
    with runtime.snapshot() as snapshot:
        assert snapshot.tools.names() == []
        assert "dev.ouro.echo/mcp_servers/calendar" not in (snapshot.generations[0].contract_hashes)
    assert any(
        item.code == "plugin_optional_contribution_unavailable"
        and "mcp_servers/calendar" in item.message
        for item in service.diagnostics("dev.ouro.echo")
    )
    assert (await service.disable("dev.ouro.echo")).status == "disabled"
    await runtime.close()


@pytest.mark.asyncio
async def test_plugin_mcp_tools_are_frozen_in_catalog_and_resource_closes(tmp_path) -> None:
    source = tmp_path / "connected-mcp"
    (source / "mcp").mkdir(parents=True)
    _write_native_plugin(
        source,
        contributes={"mcp_servers": [{"id": "calendar", "path": "./mcp/calendar.json"}]},
        extra={
            "permissions": [{"id": "process.spawn", "reason": "Start the packaged MCP server."}]
        },
    )
    shutil.copyfile(MCP_SERVER_SCRIPT, source / "server.py")
    (source / "mcp" / "calendar.json").write_text(
        json.dumps(
            {
                "command": "{python}",
                "args": ["./server.py", "--transport", "stdio"],
                "timeout_seconds": 5,
                "max_reconnect_attempts": 0,
            }
        ),
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")
    service.trust_code("dev.ouro.echo")

    assert (await asyncio.wait_for(service.enable("dev.ouro.echo"), timeout=15)).status == "active"
    resource = runtime._active["dev.ouro.echo"].resources[0]
    assert isinstance(resource, PluginMCPResource)
    with runtime.snapshot() as snapshot:
        assert snapshot.tools.names() == ["echo_calendar__echo"]
        assert "dev.ouro.echo/mcp_servers/calendar" in (snapshot.generations[0].contract_hashes)
        definition = snapshot.tools.get("echo_calendar__echo").definition
        assert definition.permission_mode == "approval"
        assert definition.metadata["mcp_server_id"] == "echo_calendar"
        assert definition.metadata["mcp_tool_name"] == "echo"
        with pytest.raises(RuntimeError, match="frozen"):
            snapshot.tools.replace((), ())
        result = await asyncio.wait_for(
            snapshot.tools.get("echo_calendar__echo").execute(
                ToolInvocation(
                    session_id="ses_mcp",
                    run_id="run_mcp",
                    tool_name="echo_calendar__echo",
                    arguments={"value": "hello"},
                )
            ),
            timeout=10,
        )
        assert result.structured == {"value": "hello"}

    assert (
        await asyncio.wait_for(service.disable("dev.ouro.echo"), timeout=10)
    ).status == "disabled"
    retirement_tasks = tuple(runtime._retirement_tasks)
    await asyncio.wait_for(
        asyncio.gather(*retirement_tasks),
        timeout=10,
    )
    assert resource.manager.get("echo_calendar").status == "stopped"
    assert resource.registry.names() == []
    with runtime.snapshot() as current:
        assert current.tools.names() == []
    await runtime.close()


def test_install_is_immutable_and_never_imports_entrypoint(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
    )
    marker = tmp_path / "imported.txt"
    (source / "echo_plugin.py").write_text(
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


def test_inprocess_manifest_requires_plugin_owned_module_namespace(tmp_path) -> None:
    source = tmp_path / "namespace"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "foreign:create_plugin"},
    )
    (source / "foreign.py").write_text("def create_plugin(context): return context\n")

    with pytest.raises(ValueError, match="plugin-owned top-level module 'echo_plugin'"):
        validate_plugin_package(source)


@pytest.mark.asyncio
async def test_inprocess_owned_package_dependency_is_unloaded_with_generation(tmp_path) -> None:
    source = tmp_path / "owned-package"
    package = source / "echo_plugin"
    package.mkdir(parents=True)
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
    )
    (package / "helper.py").write_text("VALUE = 'owned'\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        """from .helper import VALUE
from ouro.sdk.plugins.v1 import ContributionSet
class Plugin:
    async def prepare(self): return ContributionSet()
    async def start(self): assert VALUE == 'owned'
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.trust_code("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"
    with runtime.snapshot() as snapshot:
        generation_id = snapshot.generations[0].generation_id
    private_namespace = f"_ouro_plugin_{generation_id}"
    assert f"{private_namespace}.helper" in sys.modules

    await service.disable("dev.ouro.echo")
    await runtime.close()
    assert private_namespace not in sys.modules
    assert f"{private_namespace}.helper" not in sys.modules


@pytest.mark.asyncio
async def test_inprocess_loaded_top_level_namespace_conflict_rejects_activation(tmp_path) -> None:
    source = tmp_path / "namespace-conflict"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
    )
    (source / "echo_plugin.py").write_text(
        "from ouro.sdk.plugins.v1 import ContributionSet\n"
        "class Plugin:\n"
        "    async def prepare(self): return ContributionSet()\n"
        "    async def start(self): pass\n"
        "    async def stop(self, reason, deadline): pass\n"
        "def create_plugin(context): return Plugin()\n",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.trust_code("dev.ouro.echo")
    sys.modules["echo_plugin"] = ModuleType("echo_plugin")
    try:
        assert (await service.enable("dev.ouro.echo")).status == "failed"
    finally:
        sys.modules.pop("echo_plugin", None)

    assert any(
        "top-level module conflicts" in item.message
        for item in service.diagnostics("dev.ouro.echo")
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_inprocess_lifecycle_requires_separate_grants_and_drains_snapshot(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
        contributes={
            "tools": [
                {
                    "id": "run",
                    "exposed_name": "echo__run",
                    "description": "Echo input.",
                    "permission_mode": "auto",
                },
                {
                    "id": "control",
                    "exposed_name": "echo__control",
                    "description": "Control the harness.",
                    "permission_mode": "auto",
                    "permission_control": "tool",
                },
            ]
        },
    )
    (source / "echo_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet, ToolContribution, ToolOutput

class Plugin:
    def __init__(self, context):
        self.context = context

    async def prepare(self):
        async def run(call):
            return ToolOutput(content=str(call.arguments.get('value', '')))
        return ContributionSet(tools=[
            ToolContribution(
                id='run', exposed_name='echo__run', description='Echo input.',
                permission_mode='auto', handler=run,
            ),
            ToolContribution(
                id='control', exposed_name='echo__control',
                description='Control the harness.', permission_mode='auto',
                permission_control='tool', handler=run,
            ),
        ])

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
    with runtime.snapshot() as current:
        definition = current.tools.get("echo__run").definition
        assert definition.permission_mode == "auto"
        assert definition.permission_control == "host"
        assert definition.metadata["contract_hash"]
        control_definition = current.tools.get("echo__control").definition
        assert control_definition.permission_mode == "auto"
        assert control_definition.permission_control == "tool"
        preflight = ToolExecutor(
            current.tools,
            permission_overrides={"echo__control": "deny"},
        ).preflight(
            ToolInvocation(
                session_id="ses_1",
                run_id="run_1",
                tool_name="echo__control",
            ),
            approval_mode="deny",
            policy_mode="ignore",
            policy_reason=None,
        )
        assert preflight.status == "ready"
        assert preflight.invocation.permission_mode == "auto"
        result = await current.tools.get("echo__run").execute(
            ToolInvocation(
                session_id="ses_1",
                run_id="run_1",
                tool_name="echo__run",
                arguments={"value": "ok"},
            )
        )
    assert result.content == "ok"
    assert tools.names() == []

    snapshot = runtime.snapshot()
    disabling = asyncio.create_task(service.disable("dev.ouro.echo"))
    await asyncio.sleep(0)
    assert disabling.done()
    assert (await disabling).status == "disabled"
    with pytest.raises(PluginGenerationUnavailableError):
        await snapshot.tools.get("echo__run").execute(
            ToolInvocation(
                session_id="ses_1",
                run_id="run_after_disable",
                tool_name="echo__run",
                arguments={"value": "closed"},
            )
        )
    snapshot.close()
    assert tools.names() == []
    await runtime.close()
    status_path = home / "plugins" / "data" / "dev.ouro.echo" / "status.txt"
    assert status_path.read_text(encoding="utf-8") == "disable"


@pytest.mark.asyncio
async def test_immutable_catalogs_allow_nonblocking_generation_switches(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
        contributes={
            "tools": [
                {
                    "id": "run",
                    "exposed_name": "safe__run",
                    "description": "Run safely.",
                }
            ]
        },
        extra={"configuration": {"schema": "./configuration.schema.json", "version": 1}},
    )
    (source / "configuration.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (source / "echo_plugin.py").write_text(
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
    assert enabling.done()
    assert (await enabling).status == "active"
    assert empty_snapshot.tools.names() == []
    assert tools.names() == []

    first_snapshot = runtime.snapshot()
    first_generation = first_snapshot.generations[0].generation_id
    assert first_snapshot.tools.names() == ["safe__run"]
    configured = await service.configure("dev.ouro.echo", {"mode": "new"})
    assert configured.status == "active"
    with runtime.snapshot() as second_snapshot:
        assert second_snapshot.generations[0].generation_id != first_generation
        assert second_snapshot.tools.names() == ["safe__run"]
    assert first_snapshot.generations[0].generation_id == first_generation
    assert first_snapshot.tools.names() == ["safe__run"]
    first_snapshot.close()
    empty_snapshot.close()

    active_snapshot = runtime.snapshot()
    assert (await service.revoke("dev.ouro.echo")).status == "needs_authorization"
    with runtime.snapshot() as revoked_snapshot:
        assert revoked_snapshot.tools.names() == []
    assert tools.names() == []
    with pytest.raises(PluginGenerationUnavailableError):
        await active_snapshot.tools.get("safe__run").execute(
            ToolInvocation(
                session_id="ses_1",
                run_id="run_revoked",
                tool_name="safe__run",
                arguments={},
            )
        )
    active_snapshot.close()
    await runtime.close()


@pytest.mark.asyncio
async def test_declarative_skill_is_projected_into_immutable_catalog(tmp_path) -> None:
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
    runtime, _tools, observers, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"

    snapshot = runtime.snapshot()
    assert "dev.ouro.echo/skills/calendar" in snapshot.generations[0].contract_hashes
    assert snapshot.observers.registered() == []
    assert [skill.name for skill in snapshot.skills] == ["calendar"]
    skill = snapshot.skills[0]
    assert skill.description == "Manage a calendar."
    assert Path(skill.location).read_text(encoding="utf-8").endswith("Instructions.\n")
    assert skill.extensions["ouro"] == {
        "plugin_id": "dev.ouro.echo",
        "plugin_class": "user",
        "release_id": snapshot.generations[0].release_id,
        "package_digest": snapshot.generations[0].package_digest,
        "generation_id": snapshot.generations[0].generation_id,
        "contribution_id": "calendar",
        "execution_contract_hash": snapshot.generations[0].execution_contract_hash,
    }
    await service.disable("dev.ouro.echo")
    with runtime.snapshot() as current:
        assert current.skills == ()
    assert [skill.name for skill in snapshot.skills] == ["calendar"]
    assert Path(snapshot.skills[0].location).is_file()
    snapshot.close()
    assert observers.registered() == []
    await runtime.close()


@pytest.mark.asyncio
async def test_plugin_skill_name_conflicts_fail_candidate_without_overriding_host(tmp_path) -> None:
    source = tmp_path / "conflicting-skill"
    (source / "skills" / "calendar").mkdir(parents=True)
    _write_native_plugin(
        source,
        contributes={"skills": [{"id": "calendar", "path": "./skills/calendar"}]},
    )
    (source / "skills" / "calendar" / "SKILL.md").write_text(
        "---\nname: calendar\ndescription: Plugin calendar.\n---\n",
        encoding="utf-8",
    )
    host_skill = SkillMetadata(
        name="calendar",
        description="Host calendar.",
        location=str(tmp_path / "host" / "SKILL.md"),
    )
    runtime, *_ = _runtime(tmp_path / "home", base_skills=(host_skill,))
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")

    assert (await service.enable("dev.ouro.echo")).status == "failed"
    with runtime.snapshot() as snapshot:
        assert [(skill.name, skill.description) for skill in snapshot.skills] == [
            ("calendar", "Host calendar.")
        ]
        assert snapshot.generations == ()
    assert any(
        "catalog skill name conflict" in item.message
        for item in service.diagnostics("dev.ouro.echo")
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_replacing_host_skills_publishes_new_catalog_and_preserves_old_lease(
    tmp_path,
) -> None:
    original = SkillMetadata(
        name="calendar",
        description="Original calendar.",
        location=str(tmp_path / "calendar" / "SKILL.md"),
    )
    replacement = SkillMetadata(
        name="reviewer",
        description="Review changes.",
        location=str(tmp_path / "reviewer" / "SKILL.md"),
    )
    runtime, *_ = _runtime(tmp_path / "home", base_skills=(original,))
    await runtime.start()
    old = runtime.snapshot()

    await runtime.replace_base_skills((replacement,))

    with runtime.snapshot() as current:
        assert [skill.name for skill in current.skills] == ["reviewer"]
        assert current.catalog_id != old.catalog_id
    assert [skill.name for skill in old.skills] == ["calendar"]
    old.close()
    await runtime.close()


@pytest.mark.asyncio
async def test_replacing_host_skills_rejects_plugin_conflict_without_publication(
    tmp_path,
) -> None:
    source = tmp_path / "skill-plugin"
    (source / "skills" / "calendar").mkdir(parents=True)
    _write_native_plugin(
        source,
        contributes={"skills": [{"id": "calendar", "path": "./skills/calendar"}]},
    )
    (source / "skills" / "calendar" / "SKILL.md").write_text(
        "---\nname: calendar\ndescription: Plugin calendar.\n---\n",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    pending = service.show("dev.ouro.echo")
    assert pending.trust.capability_authorization == "missing"
    assert pending.trust.code_trust == "not_required"
    assert (await service.enable("dev.ouro.echo")).status == "needs_authorization"
    service.authorize("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"
    granted = service.show("dev.ouro.echo")
    assert granted.trust.capability_authorization == "granted"
    assert granted.trust.code_trust == "not_required"
    before = runtime.snapshot()
    conflicting = SkillMetadata(
        name="calendar",
        description="Host calendar.",
        location=str(tmp_path / "host" / "SKILL.md"),
    )

    with pytest.raises(ValueError, match="catalog skill name conflict"):
        await runtime.replace_base_skills((conflicting,))

    with runtime.snapshot() as current:
        assert current.catalog_id == before.catalog_id
        assert current.skills[0].description == "Plugin calendar."
    before.close()
    await runtime.close()


@pytest.mark.asyncio
async def test_host_derived_trust_rejects_irrelevant_grants(tmp_path) -> None:
    source = tmp_path / "empty-declarative"
    source.mkdir()
    _write_native_plugin(source)
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)

    detail = service.show("dev.ouro.echo")
    assert detail.trust.capability_authorization == "not_required"
    assert detail.trust.code_trust == "not_required"
    with pytest.raises(PluginOperationNotAllowedError, match="no capabilities"):
        service.authorize("dev.ouro.echo")
    with pytest.raises(PluginOperationNotAllowedError, match="does not require code trust"):
        service.trust_code("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"
    await runtime.close()


@pytest.mark.asyncio
async def test_executable_plugin_cannot_activate_without_exact_code_trust(tmp_path) -> None:
    source = tmp_path / "empty-executable"
    import_marker = tmp_path / "plugin-imported.txt"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
        extra={
            "permissions": [
                {"id": "network.outbound", "reason": "Exercise the authorization gate."}
            ]
        },
    )
    (source / "echo_plugin.py").write_text(
        f"""from pathlib import Path
from ouro.sdk.plugins.v1 import ContributionSet
Path({json.dumps(str(import_marker))}).write_text('imported', encoding='utf-8')
class Plugin:
    def __init__(self, context):
        assert 'network.outbound' in context.permissions
    async def prepare(self): return ContributionSet()
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin(context)
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)

    pending = service.show("dev.ouro.echo")
    assert pending.trust.capability_authorization == "missing"
    assert pending.trust.code_trust == "missing"
    assert (await service.enable("dev.ouro.echo")).status == "needs_code_trust"
    assert not import_marker.exists()
    with runtime.snapshot() as catalog:
        assert catalog.generations == ()
    service.trust_code("dev.ouro.echo")
    assert service.show("dev.ouro.echo").trust.code_trust == "granted"
    assert (await service.enable("dev.ouro.echo")).status == "needs_authorization"
    assert not import_marker.exists()
    service.authorize("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"
    assert import_marker.read_text(encoding="utf-8") == "imported"
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared_id", "skill_name", "expected"),
    [
        ("calendar", "weather", "does not match contribution id"),
        ("calendar", "calendar", "must resolve to exactly one skill"),
    ],
)
async def test_plugin_skill_declaration_resolves_exactly_one_matching_skill(
    tmp_path,
    declared_id,
    skill_name,
    expected,
) -> None:
    source = tmp_path / f"skill-{skill_name}"
    skill_root = source / "skills"
    (skill_root / "first").mkdir(parents=True)
    _write_native_plugin(
        source,
        contributes={"skills": [{"id": declared_id, "path": "./skills"}]},
    )
    (skill_root / "first" / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: First.\n---\n",
        encoding="utf-8",
    )
    if skill_name == declared_id:
        (skill_root / "second").mkdir()
        (skill_root / "second" / "SKILL.md").write_text(
            "---\nname: second\ndescription: Second.\n---\n",
            encoding="utf-8",
        )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")

    assert (await service.enable("dev.ouro.echo")).status == "failed"
    assert any(expected in item.message for item in service.diagnostics("dev.ouro.echo"))
    await runtime.close()


@pytest.mark.asyncio
async def test_commands_and_background_services_use_owned_lifecycle(tmp_path) -> None:
    source = tmp_path / "service-plugin"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
        contributes={
            "commands": [
                {
                    "id": "hello",
                    "description": "Say hello.",
                    "usage": "[formal|casual]",
                    "argument_options": [
                        {"value": "formal", "description": "Use a formal greeting."},
                        {"value": "casual", "description": "Use a casual greeting."},
                    ],
                }
            ],
            "background_services": [{"id": "watcher"}],
        },
    )
    (source / "echo_plugin.py").write_text(
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
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.echo")
    service.trust_code("dev.ouro.echo")
    assert (await service.enable("dev.ouro.echo")).status == "active"

    command_view = service.list_commands()[0]
    assert command_view.command_id == "dev.ouro.echo:hello"
    assert command_view.usage == "[formal|casual]"
    assert [item.value for item in command_view.argument_options] == ["formal", "casual"]
    with runtime.snapshot() as snapshot:
        contracts = snapshot.generations[0].contract_hashes
        assert "dev.ouro.echo/background_services/watcher" in contracts
    result = await service.execute_command("dev.ouro.echo:hello", "world")
    assert result.output == "hello world"
    status = home / "plugins" / "data" / "dev.ouro.echo" / "watcher.txt"
    assert status.read_text(encoding="utf-8") == "started"
    await service.disable("dev.ouro.echo")
    assert service.list_commands() == []
    await runtime.close()
    assert status.read_text(encoding="utf-8") == "stopped"


def test_candidate_catalog_failure_does_not_mutate_host_registries(tmp_path) -> None:
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
        observers=ObserverRegistry(),
        commands=commands,
    )
    generation = PluginGenerationRecord(
        generation_id="plg_test",
        plugin_id=installed.plugin_id,
        version=installed.version,
        package_digest=installed.package_digest,
        runtime_kind="declarative",
        configuration_revision_id="1" * 64,
        dependency_lock_fingerprint="2" * 64,
        plugin_api_version="2.0.0",
    )
    prepared = coordinator.prepare(
        _user_candidate(installed, source),
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

    base = ExtensionCatalogBase.capture(
        tools=tools,
        observers=coordinator.observers,
        commands=commands,
        compaction=None,
        continuations=None,
        tool_policies=None,
    )
    with pytest.raises(ValueError, match="already registered"):
        build_extension_catalog(base, (_CatalogGeneration(generation, prepared),))

    assert tools.names() == []
    assert [item.command_id for item in commands.list()] == ["dev.ouro.echo:go"]


def test_failed_candidate_catalog_leaves_previous_catalog_immutable() -> None:
    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(name="host__run", description="Host run."),
            handler=lambda invocation: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
            ),
        )
    )
    base = ExtensionCatalogBase.capture(
        tools=tools,
        observers=ObserverRegistry(),
        commands=None,
        compaction=None,
        continuations=None,
        tool_policies=None,
    )
    previous_owner = ContributionOwner(
        plugin_id="dev.ouro.echo",
        plugin_class="user",
        release_id="digest-1",
        generation_id="generation-1",
        package_digest="digest-1",
    )
    previous_record = PluginGenerationRecord(
        generation_id="generation-1",
        plugin_id="dev.ouro.echo",
        version="1.0.0",
        package_digest="digest-1",
        runtime_kind="declarative",
        configuration_revision_id="1" * 64,
        dependency_lock_fingerprint="2" * 64,
        plugin_api_version="2.0.0",
    )
    previous_catalog = build_extension_catalog(
        base,
        (
            _CatalogGeneration(
                previous_record,
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
                ),
            ),
        ),
    )

    replacement = PreparedContributionSet(
        owner=ContributionOwner(
            plugin_id="dev.ouro.echo",
            plugin_class="user",
            release_id="digest-2",
            generation_id="generation-2",
            package_digest="digest-2",
        ),
        tools=(
            Tool(
                definition=ToolDefinition(name="host__run", description="Conflict."),
                handler=lambda invocation: ToolResult(
                    tool_call_id=invocation.tool_call_id,
                    tool_name=invocation.tool_name,
                ),
            ),
        ),
    )
    replacement_record = PluginGenerationRecord(
        generation_id="generation-2",
        plugin_id="dev.ouro.echo",
        version="2.0.0",
        package_digest="digest-2",
        runtime_kind="declarative",
        configuration_revision_id="3" * 64,
        dependency_lock_fingerprint="4" * 64,
        plugin_api_version="2.0.0",
    )
    with pytest.raises(ValueError, match="tool already registered"):
        build_extension_catalog(base, (_CatalogGeneration(replacement_record, replacement),))

    assert previous_catalog.tools.names() == ["echo__run", "host__run"]
    with pytest.raises(RuntimeError, match="frozen"):
        previous_catalog.tools.unregister(("echo__run",))
    assert tools.names() == ["host__run"]


@pytest.mark.asyncio
async def test_new_catalog_lease_rebases_changed_host_tools_without_mutating_old_lease(
    tmp_path,
) -> None:
    runtime, tools, *_ = _runtime(tmp_path)
    await runtime.start()
    old_lease = runtime.snapshot()
    try:
        tools.register(
            Tool(
                definition=ToolDefinition(
                    name="live__search",
                    description="Search through a live MCP connection.",
                    source="mcp",
                ),
                handler=lambda invocation: ToolResult(
                    tool_call_id=invocation.tool_call_id,
                    tool_name=invocation.tool_name,
                ),
            )
        )

        current_lease = runtime.snapshot()
        try:
            assert old_lease.tools.names() == []
            assert current_lease.tools.names() == ["live__search"]
            assert current_lease.catalog_id != old_lease.catalog_id
        finally:
            current_lease.close()
    finally:
        old_lease.close()
        await runtime.close()


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
async def test_worker_timeout_and_task_cancellation_send_protocol_cancel(tmp_path) -> None:
    (tmp_path / "worker.py").write_text(
        """import json, pathlib, sys
log = pathlib.Path('events.log')
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'cancel':
        with log.open('a', encoding='utf-8') as stream:
            stream.write('cancel:' + str(request['params']['id']) + '\\n')
        continue
    if method == 'shutdown':
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {}}), flush=True)
        break
""",
        encoding="utf-8",
    )
    diagnostics: list[tuple[str, str]] = []
    client = JsonRpcWorkerClient(
        ["{python}", "./worker.py"],
        cwd=tmp_path,
        diagnostic=lambda code, message: diagnostics.append((code, message)),
    )

    with pytest.raises(TimeoutError):
        await client.request("slow", timeout=0.05)
    pending = asyncio.create_task(client.request("slow"))
    await asyncio.sleep(0.05)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    log = tmp_path / "events.log"
    for _ in range(50):
        if log.exists() and len(log.read_text(encoding="utf-8").splitlines()) == 2:
            break
        await asyncio.sleep(0.01)
    assert log.read_text(encoding="utf-8").splitlines() == ["cancel:1", "cancel:2"]
    await client.close(timeout=0.2)
    assert client.process is None
    assert diagnostics == []


@pytest.mark.asyncio
async def test_worker_prompt_policy_timeout_is_fail_closed_and_sends_cancel(tmp_path) -> None:
    source = tmp_path / "worker-prompt-cancel"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.worker_prompt_cancel",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "2.0",
        },
        contributes={
            "prompt_policies": [
                {
                    "id": "guard",
                    "timeout_seconds": 0.05,
                }
            ]
        },
        extra={
            "permissions": [
                {
                    "id": "sessions.prompt.inspect",
                    "reason": "Exercise worker Prompt Policy cancellation.",
                }
            ]
        },
    )
    (source / "worker.py").write_text(
        """import json, pathlib, sys
cancel_log = None
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'cancel':
        with cancel_log.open('a', encoding='utf-8') as stream:
            stream.write(str(request['params']['id']) + '\\n')
        continue
    result = {}
    if method == 'initialize':
        cancel_log = pathlib.Path(request['params']['data_directory']) / 'cancel.log'
        result = {'protocol_version': '2.0'}
    elif method == 'contributions.prepare':
        result = {'prompt_policies': ['guard']}
    elif method == 'prompt_policy.call':
        if request['params']['call']['prompt'] == 'invalid response':
            result = {'action': 'rewrite'}
        else:
            continue
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown':
        break
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.worker_prompt_cancel")
    service.trust_code("dev.ouro.worker_prompt_cancel")
    assert (await service.enable("dev.ouro.worker_prompt_cancel")).status == "active"
    snapshot = runtime.snapshot()

    evaluation = await snapshot.prompt_policies.evaluate(
        PromptPolicyCall(
            session_id="ses_worker",
            prospective_run_id="run_worker",
            prompt="inspect me",
            catalog_id=snapshot.catalog_id,
        )
    )

    assert evaluation.allowed is False
    assert evaluation.failures[0].error_type == "TimeoutError"
    cancel_log = (
        tmp_path / "home" / "plugins" / "data" / "dev.ouro.worker_prompt_cancel" / "cancel.log"
    )
    for _ in range(50):
        if cancel_log.exists() and cancel_log.stat().st_size:
            break
        await asyncio.sleep(0.01)
    assert cancel_log.read_text(encoding="utf-8").splitlines() == ["4"]

    invalid = await snapshot.prompt_policies.evaluate(
        PromptPolicyCall(
            session_id="ses_worker",
            prospective_run_id="run_worker_invalid",
            prompt="invalid response",
            catalog_id=snapshot.catalog_id,
        )
    )
    assert invalid.allowed is False
    assert invalid.failures[0].error_type == "WorkerProtocolError"

    snapshot.close()
    await service.disable("dev.ouro.worker_prompt_cancel")
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message_limit", "expected"),
    [
        ("debug output", 1024, "invalid JSON"),
        (
            '{"jsonrpc":"2.0","id":1,"result":{},"error":{}}',
            1024,
            "exactly one",
        ),
        (
            '{"jsonrpc":"2.0","id":1,"result":{"value":"' + "x" * 256 + '"}}',
            128,
            "message limit",
        ),
    ],
)
async def test_worker_stdout_pollution_and_oversize_are_fatal_bounded_diagnostics(
    tmp_path,
    response: str,
    message_limit: int,
    expected: str,
) -> None:
    (tmp_path / "worker.py").write_text(
        f"import sys\nfor _line in sys.stdin:\n    print({response!r}, flush=True)\n",
        encoding="utf-8",
    )
    diagnostics: list[tuple[str, str]] = []
    client = JsonRpcWorkerClient(
        ["{python}", "./worker.py"],
        cwd=tmp_path,
        diagnostic=lambda code, message: diagnostics.append((code, message)),
        max_message_bytes=message_limit,
    )

    with pytest.raises(WorkerProtocolError):
        await client.request("probe", timeout=1.0)
    assert diagnostics[-1][0] == "worker_exit"
    assert expected in diagnostics[-1][1]
    assert len(diagnostics[-1][1]) <= 4096
    await client.close(timeout=0.2)


@pytest.mark.asyncio
async def test_worker_shutdown_terminates_descendant_processes(tmp_path) -> None:
    (tmp_path / "child.py").write_text(
        "import pathlib, time\n"
        "path = pathlib.Path('child-ticks.log')\n"
        "while True:\n"
        "    with path.open('a', encoding='utf-8') as stream: stream.write('tick\\n')\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    (tmp_path / "worker.py").write_text(
        """import json, subprocess, sys
child = None
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'spawn':
        child = subprocess.Popen([sys._base_executable, 'child.py'])
        with open('child.pid', 'w', encoding='ascii') as stream: stream.write(str(child.pid))
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {}}), flush=True)
""",
        encoding="utf-8",
    )
    client = JsonRpcWorkerClient(
        ["{python}", "./worker.py"],
        cwd=tmp_path,
        diagnostic=lambda _code, _message: None,
    )
    pid_path = tmp_path / "child.pid"
    try:
        await client.request("spawn")
        for _ in range(50):
            if pid_path.exists():
                break
            await asyncio.sleep(0.01)
        child_pid = int(pid_path.read_text(encoding="ascii"))
        assert _process_is_running(child_pid)
    finally:
        await client.close(timeout=0.2)
    for _ in range(50):
        if not _process_is_running(child_pid):
            break
        await asyncio.sleep(0.01)
    try:
        assert not _process_is_running(child_pid)
    finally:
        _terminate_test_process(child_pid)


@pytest.mark.asyncio
async def test_worker_stderr_diagnostics_are_redacted_and_bounded(tmp_path) -> None:
    secret = "worker-secret-value"
    (tmp_path / "worker.py").write_text(
        f"""import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(({secret!r} + 'x' * 5000), file=sys.stderr, flush=True)
    if 'id' in request:
        print(json.dumps({{'jsonrpc': '2.0', 'id': request['id'], 'result': {{}}}}), flush=True)
    if request.get('method') == 'shutdown': break
""",
        encoding="utf-8",
    )
    diagnostics: list[tuple[str, str]] = []
    client = JsonRpcWorkerClient(
        ["{python}", "./worker.py"],
        cwd=tmp_path,
        diagnostic=lambda code, message: diagnostics.append((code, message)),
        redaction_values=(secret,),
    )
    await client.request("probe")
    for _ in range(50):
        if any(code == "worker_stderr" for code, _message in diagnostics):
            break
        await asyncio.sleep(0.01)

    stderr = [message for code, message in diagnostics if code == "worker_stderr"]
    assert stderr
    assert all(secret not in message and len(message) <= 4096 for message in stderr)
    await client.close(timeout=0.2)


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
            "protocol": "2.0",
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
        result = {'protocol_version': '2.0'}
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
    with runtime.snapshot() as snapshot:
        result = await snapshot.tools.get("worker__echo").execute(
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
async def test_worker_tool_can_return_structured_input_request(tmp_path) -> None:
    source = tmp_path / "worker-input"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.worker_input",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "2.0",
        },
        contributes={
            "tools": [
                {
                    "id": "confirm",
                    "exposed_name": "worker__confirm",
                    "description": "Request plan confirmation.",
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
        result = {'protocol_version': '2.0'}
    elif method == 'contributions.prepare':
        result = {'tools': {'confirm': {}}}
    elif method == 'tool.call':
        result = {'input_request': {'type': 'choice', 'options': ['yes', 'no']}}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown': break
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.worker_input")
    service.trust_code("dev.ouro.worker_input")
    assert (await service.enable("dev.ouro.worker_input")).status == "active"

    with runtime.snapshot() as snapshot:
        result = await snapshot.tools.get("worker__confirm").execute(
            ToolInvocation(
                session_id="ses_worker",
                run_id="run_worker",
                tool_name="worker__confirm",
            )
        )

    assert isinstance(result, ToolInputRequest)
    assert result.data == {"type": "choice", "options": ["yes", "no"]}
    await service.disable("dev.ouro.worker_input")
    await runtime.close()


@pytest.mark.asyncio
async def test_worker_resumable_tool_receives_unattended_resolution_contract(tmp_path) -> None:
    source = tmp_path / "worker-resume"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.worker_resume",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "2.0",
        },
        contributes={
            "tools": [
                {
                    "id": "confirm",
                    "exposed_name": "worker__resume",
                    "description": "Resume through worker policy.",
                    "resumable": True,
                    "supports_unattended": True,
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
        result = {'protocol_version': '2.0'}
    elif method == 'contributions.prepare':
        result = {'tools': {'confirm': {}}}
    elif method == 'tool.call':
        result = {'input_request': {'type': 'confirm'}}
    elif method == 'tool.resume':
        resolution = request['params']['call']['resolution']
        result = {'content': resolution['kind'], 'structured': resolution}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown': break
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.worker_resume")
    service.trust_code("dev.ouro.worker_resume")
    assert (await service.enable("dev.ouro.worker_resume")).status == "active"

    with runtime.snapshot() as snapshot:
        tool = snapshot.tools.get("worker__resume")
        invocation = ToolInvocation(
            tool_call_id="call_resume",
            session_id="ses_worker",
            run_id="run_worker",
            tool_name="worker__resume",
        )
        request = await tool.execute(invocation)
        assert isinstance(request, ToolInputRequest)
        result = await tool.resume(
            invocation,
            UnattendedResolution(
                resolver_id="ouro.afk",
                reason="AFK mode does not wait for user input",
            ),
            request=request,
        )

    assert isinstance(result, ToolResult)
    assert result.content == "unattended"
    assert result.structured["resolved_by"] == "host_policy"
    assert result.structured["resolver_id"] == "ouro.afk"
    await service.disable("dev.ouro.worker_resume")
    await runtime.close()


@pytest.mark.asyncio
async def test_worker_invalid_call_result_is_bounded_diagnostic(tmp_path) -> None:
    source = tmp_path / "worker-invalid-result"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.worker_invalid",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "2.0",
        },
        contributes={
            "tools": [
                {
                    "id": "run",
                    "exposed_name": "worker_invalid__run",
                    "description": "Return an invalid result.",
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
    if method == 'initialize': result = {'protocol_version': '2.0'}
    elif method == 'contributions.prepare': result = {'tools': {'run': {}}}
    elif method == 'tool.call': result = {'resource_refs': 'not-an-array'}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown': break
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.worker_invalid")
    service.trust_code("dev.ouro.worker_invalid")
    assert (await service.enable("dev.ouro.worker_invalid")).status == "active"

    with runtime.snapshot() as snapshot:
        with pytest.raises(WorkerProtocolError, match="resource_refs"):
            await snapshot.tools.get("worker_invalid__run").execute(
                ToolInvocation(
                    session_id="ses_invalid",
                    run_id="run_invalid",
                    tool_name="worker_invalid__run",
                    arguments={},
                )
            )
    diagnostic = next(
        item
        for item in service.diagnostics("dev.ouro.worker_invalid")
        if item.code == "worker_invalid_result"
    )
    assert "resource_refs" in diagnostic.message
    assert len(diagnostic.message) <= 4096
    assert service.show("dev.ouro.worker_invalid").status == "active"
    await runtime.close()


@pytest.mark.asyncio
async def test_worker_exit_withdraws_generation_and_projects_failed(tmp_path) -> None:
    source = tmp_path / "worker-exit"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.worker_exit",
        runtime={
            "kind": "worker",
            "command": ["{python}", "./worker.py"],
            "protocol": "2.0",
        },
        contributes={
            "tools": [
                {
                    "id": "run",
                    "exposed_name": "worker_exit__run",
                    "description": "Run before exit.",
                }
            ]
        },
    )
    (source / "worker.py").write_text(
        """import json, os, sys, threading, time
def exit_soon():
    time.sleep(0.05)
    os._exit(0)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    result = {}
    if method == 'initialize':
        result = {'protocol_version': '2.0'}
    elif method == 'contributions.prepare':
        result = {'tools': {'run': {}}}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'lifecycle.start':
        threading.Thread(target=exit_soon, daemon=True).start()
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.worker_exit")
    service.trust_code("dev.ouro.worker_exit")
    assert (await service.enable("dev.ouro.worker_exit")).status == "active"
    old_catalog = runtime.snapshot()

    for _ in range(50):
        if service.show("dev.ouro.worker_exit").status == "failed":
            break
        await asyncio.sleep(0.01)
    assert service.show("dev.ouro.worker_exit").status == "failed"
    with runtime.snapshot() as current:
        assert current.generations == ()
    with pytest.raises(PluginGenerationUnavailableError):
        await old_catalog.tools.get("worker_exit__run").execute(
            ToolInvocation(
                session_id="ses_1",
                run_id="run_1",
                tool_name="worker_exit__run",
                arguments={},
            )
        )
    old_catalog.close()
    assert any(item.code == "worker_exit" for item in service.diagnostics("dev.ouro.worker_exit"))
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
            "protocol": "2.0",
        },
        contributes={
            "commands": [{"id": "goal", "description": "Start a goal."}],
            "observers": [
                {"id": "events", "event": "runtime_event"},
                {"id": "context", "event": "context_prepared"},
                {"id": "requested", "event": "tool_call_requested"},
                {"id": "finished", "event": "tool_call_finished"},
                {"id": "runs", "event": "run_finished"},
            ],
            "context_providers": [
                {
                    "id": "goal_context",
                    "priority": 5,
                    "failure_mode": "block",
                }
            ],
            "continuation_policies": [{"id": "goal"}],
            "prompt_policies": [{"id": "prompt_guard"}],
            "session_controllers": [
                {
                    "id": "mode",
                    "description": "Own a worker-backed Session mode.",
                    "claims": [{"domain": "dev.ouro.worker_goal.mode"}],
                }
            ],
            "turn_policies": [{"id": "goal_completion", "priority": 7}],
            "tool_policies": [{"id": "guard"}],
        },
        extra={
            "permissions": [
                {"id": "sessions.write", "reason": "Write goal input."},
                {"id": "background.run", "reason": "Continue the goal."},
                {
                    "id": "sessions.prompt.inspect",
                    "reason": "Inspect prompts before persistence.",
                },
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
        result = {'protocol_version': '2.0'}
    elif method == 'contributions.prepare':
        result = {
            'commands': ['goal'],
            'observers': ['events', 'context', 'requested', 'finished', 'runs'],
            'context_providers': ['goal_context'],
            'continuation_policies': ['goal'],
            'prompt_policies': ['prompt_guard'],
            'session_controllers': ['mode'],
            'turn_policies': ['goal_completion'],
            'tool_policies': ['guard'],
        }
    elif method == 'command.call':
        argument = request['params']['call']['argument']
        result = {
            'output': 'started',
            'session_input_batches': [{
                'coalesce_key': 'goal-control',
                'inputs': [{
                    'prompt': 'Goal: ' + argument,
                    'mode': 'follow_up',
                    'role': 'user',
                    'semantic_tag': 'goal-create',
                }],
            }],
        }
    elif method == 'continuation.call':
        if request['params']['call']['status'] == 'completed':
            result = {
                'action': 'continue',
                'prompt': 'Continue goal',
                'role': 'user',
                'semantic_tag': 'goal-continue',
                'coalesce_key': 'goal-control',
                'state_revision': 3,
            }
        else:
            result = {
                'action': 'pause',
                'reason': 'terminal run',
                'session_control_transitions': [{
                    'action': 'release',
                    'controller_id': 'mode',
                    'payload': {'status': 'paused'},
                }],
            }
    elif method == 'session_controller.call':
        call = request['params']['call']
        result = {'data': {
            'session_kind': call['session_kind'],
            'payload': call['payload'],
        }}
    elif method == 'context.call':
        result = {
            'fragments': [
                {
                    'text': 'Current goal: ship',
                    'persistence': 'message',
                    'semantic_tag': 'worker-context',
                },
            ]
        }
    elif method == 'turn_policy.call':
        result = {'action': 'continue', 'feedback': 'Continue the current goal'}
    elif method == 'tool_policy.call':
        result = {'action': 'deny', 'reason': 'worker guard denied the call'}
    elif method == 'prompt_policy.call':
        call = request['params']['call']
        result = {
            'action': 'deny' if 'blocked' in call['prompt'] else 'allow',
            'reason': (
                'worker prompt guard denied the input'
                if 'blocked' in call['prompt'] else None
            ),
        }
    elif method == 'observer.observe':
        observation = request['params']['observation']
        required = {
            'runtime_event': 'record',
            'context_prepared': 'message_count',
            'tool_call_requested': 'arguments',
            'tool_call_finished': 'structured',
            'run_finished': 'outcome',
        }
        if required.get(observation.get('event')) not in observation:
            result = {'unexpected': observation}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown':
        break
""",
        encoding="utf-8",
    )
    runtime, _, _, commands, _ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.worker_goal")
    service.trust_code("dev.ouro.worker_goal")

    assert (await service.enable("dev.ouro.worker_goal")).status == "active"
    command = await service.execute_command("dev.ouro.worker_goal:goal", "ship")
    assert command.session_inputs == []
    assert command.session_input_batches[0].inputs[0].prompt == "Goal: ship"
    assert command.session_input_batches[0].inputs[0].role == "user"
    assert command.session_input_batches[0].inputs[0].semantic_tag == "goal-create"
    snapshot = runtime.snapshot()
    policy = snapshot.continuations.registered()[0]
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
    assert decision.role == "user"
    assert decision.semantic_tag == "goal-continue"
    assert decision.coalesce_key == "goal-control"
    assert decision.state_revision == 3
    terminal_decision = await policy.handler(
        RunOutcome(
            session_id="ses_worker",
            run_id="run_worker_failed",
            status="failed",
            origin="user",
        )
    )
    assert terminal_decision.action == "pause"
    assert terminal_decision.session_control_transitions[0].controller_id == "mode"
    assert terminal_decision.session_control_transitions[0].payload == {"status": "paused"}
    controller = snapshot.session_controllers.get("dev.ouro.worker_goal/session_controllers/mode")
    activated = await controller.activate(
        session_id="ses_worker_child",
        session_kind="subagent",
        activation_id="activation_worker",
        payload={"phase": "start"},
    )
    deactivated = await controller.deactivate(
        session_id="ses_worker_child",
        session_kind="subagent",
        activation_id="activation_worker",
        payload={"phase": "end"},
    )
    assert activated.data == {
        "session_kind": "subagent",
        "payload": {"phase": "start"},
    }
    assert deactivated.data == {
        "session_kind": "subagent",
        "payload": {"phase": "end"},
    }
    context_provider = snapshot.context_providers.registered()[0]
    context_result = await context_provider.handler(
        ContextProviderCall(
            session_id="ses_worker",
            run_id="run_worker",
            message_count=1,
            model_round=1,
        )
    )
    assert [fragment.text for fragment in context_result.fragments] == ["Current goal: ship"]
    assert context_result.fragments[0].persistence == "message"
    assert context_result.fragments[0].semantic_tag == "worker-context"
    assert context_provider.failure_mode == "block"
    turn_policy = snapshot.turn_policies.registered()[0]
    turn_decision = await turn_policy.handler(
        TurnCompletionCandidate(
            session_id="ses_worker",
            run_id="run_worker",
            turn_id="turn_worker",
            assistant_text="candidate",
        )
    )
    assert turn_decision.action == "continue"
    assert turn_decision.feedback == "Continue the current goal"
    tool_policy = snapshot.tool_policies.registered()[0]
    tool_decision = await tool_policy.handler(
        ToolPolicyCall(
            session_id="ses_worker",
            run_id="run_worker",
            tool_call_id="call_worker",
            tool_name="Bash",
            canonical_tool_id="ouro.host/tools/Bash",
            owner_id="ouro.host",
            arguments={"command": "unsafe"},
        )
    )
    assert tool_decision.action == "deny"
    assert tool_decision.reason == "worker guard denied the call"
    prompt_policy = snapshot.prompt_policies.registered()[0]
    prompt_decision = await prompt_policy.handler(
        PromptPolicyCall(
            session_id="ses_worker",
            prospective_run_id="run_worker",
            prompt="blocked input",
            catalog_id=snapshot.catalog_id,
        )
    )
    assert prompt_decision.action == "deny"
    assert prompt_decision.reason == "worker prompt guard denied the input"
    observations = (
        RuntimeEventObservation(
            session_id="ses_worker",
            run_id="run_worker",
            record=RuntimeEventView(
                event_id="evt_worker",
                session_id="ses_worker",
                run_id="run_worker",
                turn_id="turn_worker",
                event_type="run_started",
                created_at=utc_now(),
                sequence=1,
            ),
        ),
        ContextPreparedObservation(
            session_id="ses_worker",
            run_id="run_worker",
            message_count=3,
            tool_count=2,
            model_round=1,
        ),
        ToolCallRequestedObservation(
            session_id="ses_worker",
            run_id="run_worker",
            tool_call_id="call_worker",
            tool_name="Read",
            canonical_tool_id="ouro.host/tools/Read",
            owner_id="ouro.host",
            effect="read_only",
            batch_behavior="compatible",
        ),
        ToolCallFinishedObservation(
            session_id="ses_worker",
            run_id="run_worker",
            tool_call_id="call_worker",
            tool_name="Read",
            canonical_tool_id="ouro.host/tools/Read",
            owner_id="ouro.host",
            effect="read_only",
            batch_behavior="compatible",
            is_error=False,
            content="done",
        ),
        RunFinishedObservation(
            session_id="ses_worker",
            run_id="run_worker",
            outcome=RunOutcomeView(
                session_id="ses_worker",
                run_id="run_worker",
                status="completed",
                origin="user",
                final_text="done",
            ),
        ),
    )
    for observation in observations:
        observer = snapshot.observers.registered(observation.event)[0]
        await observer.handler(observation)
    snapshot.close()
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
            "protocol": "2.0",
        },
    )
    (source / "worker.py").write_text(
        """import json, sys
for line in sys.stdin:
    request = json.loads(line)
    result = {'protocol_version': '1.3'} if request.get('method') == 'initialize' else {}
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
            "protocol": "2.0",
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
            result = {'protocol_version': '2.0'}
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
        runtime={
            "kind": "python-inprocess",
            "entrypoint": "inprocess_credentials_plugin:create_plugin",
        },
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
    (source / "inprocess_credentials_plugin.py").write_text(
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
async def test_unreadable_plugin_state_degrades_startup_to_empty_catalog(tmp_path) -> None:
    runtime, *_ = _runtime(tmp_path / "home")
    runtime.store.initialize()
    runtime.store.state_path.write_text(
        json.dumps({"schema_version": 1, "plugins": {}}),
        encoding="utf-8",
    )

    await runtime.start()

    with runtime.snapshot() as snapshot:
        assert list(snapshot.generations) == []
    diagnostic = next(item for item in runtime.diagnostics() if item.code == "plugin_state_reset")
    assert diagnostic.phase == "startup"
    assert any(event.action == "state_reset" for event in runtime.store.audit_events())
    await runtime.close()


@pytest.mark.asyncio
async def test_plugin_runtime_can_retry_after_transient_state_io_failure(
    tmp_path,
    monkeypatch,
) -> None:
    runtime, *_ = _runtime(tmp_path / "home")
    original_recover = runtime.store.recover_state
    attempts = 0

    def recover_state():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PluginStoreIOError("plugin state is temporarily unavailable")
        return original_recover()

    monkeypatch.setattr(runtime.store, "recover_state", recover_state)

    with pytest.raises(PluginStoreIOError, match="temporarily unavailable"):
        await runtime.start()
    await runtime.start()

    assert attempts == 2
    with runtime.snapshot() as snapshot:
        assert list(snapshot.generations) == []
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
async def test_failed_dependency_blocks_dependant_and_retry_recovers_closure(tmp_path) -> None:
    dependency_v1 = tmp_path / "dependency-v1"
    dependency_v2 = tmp_path / "dependency-v2"
    dependant = tmp_path / "dependant"
    unrelated = tmp_path / "unrelated"
    for source in (dependency_v1, dependency_v2, dependant, unrelated):
        source.mkdir()
    _write_native_plugin(
        dependency_v1,
        plugin_id="dev.ouro.dependency",
        version="1.0.0",
        runtime={"kind": "python-inprocess", "entrypoint": "dependency_plugin:create_plugin"},
    )
    (dependency_v1 / "dependency_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet
class Plugin:
    async def prepare(self): return ContributionSet()
    async def start(self): raise RuntimeError('dependency failed')
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    _write_native_plugin(
        dependency_v2,
        plugin_id="dev.ouro.dependency",
        version="1.1.0",
    )
    _write_native_plugin(
        dependant,
        plugin_id="dev.ouro.dependant",
        dependencies=[{"plugin_id": "dev.ouro.dependency", "version": "^1.0.0"}],
    )
    _write_native_plugin(unrelated, plugin_id="dev.ouro.unrelated")
    home = tmp_path / "home"
    runtime, *_ = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime)
    first = service.install(dependency_v1)
    second = service.install(dependency_v2)
    service.install(dependant)
    service.install(unrelated)
    service.trust_code("dev.ouro.dependency", package_digest=first.package_digest)

    assert (await service.enable("dev.ouro.dependant")).status == "blocked"
    assert service.show("dev.ouro.dependency").status == "failed"
    assert service.show("dev.ouro.dependant").status == "blocked"
    assert service.check("dev.ouro.dependant").status == "blocked"
    assert any(
        item.code == "plugin_dependency_blocked"
        for item in service.diagnostics("dev.ouro.dependant")
    )
    assert (await service.enable("dev.ouro.unrelated")).status == "active"
    assert all(
        runtime.store.read_state().plugins[plugin_id].activation.desired_enabled
        for plugin_id in ("dev.ouro.dependency", "dev.ouro.dependant", "dev.ouro.unrelated")
    )
    await runtime.close()

    restarted, *_ = _runtime(home)
    restarted_service = PluginService(restarted.store, runtime=restarted)
    await restarted.start()
    assert restarted_service.show("dev.ouro.unrelated").status == "active"
    assert restarted_service.show("dev.ouro.dependant").status == "blocked"

    selected = await restarted_service.select("dev.ouro.dependency", second.package_digest)
    assert selected.status == "active"
    assert restarted_service.show("dev.ouro.dependant").status == "active"
    with restarted.snapshot() as catalog:
        assert {item.plugin_id for item in catalog.generations} == {
            "dev.ouro.dependency",
            "dev.ouro.dependant",
            "dev.ouro.unrelated",
        }
    assert not any(
        item.code == "plugin_dependency_blocked"
        for item in restarted_service.diagnostics("dev.ouro.dependant")
    )
    await restarted.close()


@pytest.mark.asyncio
async def test_supervised_task_failure_projects_degraded_consistently(tmp_path) -> None:
    source = tmp_path / "degraded"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.degraded",
        runtime={"kind": "python-inprocess", "entrypoint": "degraded_plugin:create_plugin"},
    )
    (source / "degraded_plugin.py").write_text(
        """import asyncio
from ouro.sdk.plugins.v1 import ContributionSet
class Plugin:
    def __init__(self, context): self.context = context
    async def prepare(self): return ContributionSet()
    async def fail(self):
        await asyncio.sleep(0)
        raise RuntimeError('background failed')
    async def start(self): self.context.tasks.start(self.fail(), name='fail')
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin(context)
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.trust_code("dev.ouro.degraded")
    assert (await service.enable("dev.ouro.degraded")).status == "active"
    await asyncio.sleep(0.01)

    assert service.show("dev.ouro.degraded").status == "degraded"
    assert service.check("dev.ouro.degraded").status == "degraded"
    assert service.list()[0].status == "degraded"
    assert any(
        item.code == "plugin_background_task_failed"
        for item in service.diagnostics("dev.ouro.degraded")
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_missing_optional_contribution_projects_degraded(tmp_path) -> None:
    source = tmp_path / "optional"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.optional",
        runtime={"kind": "python-inprocess", "entrypoint": "optional_plugin:create_plugin"},
        contributes={
            "tools": [
                {
                    "id": "maybe",
                    "exposed_name": "optional__maybe",
                    "description": "May be unavailable.",
                    "optional": True,
                }
            ]
        },
    )
    (source / "optional_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet
class Plugin:
    async def prepare(self): return ContributionSet()
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin()
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.optional")
    service.trust_code("dev.ouro.optional")

    assert (await service.enable("dev.ouro.optional")).status == "degraded"
    assert service.show("dev.ouro.optional").status == "degraded"
    assert service.check("dev.ouro.optional").status == "degraded"
    assert any(
        item.code == "plugin_optional_contribution_unavailable" and "tools/maybe" in item.message
        for item in service.diagnostics("dev.ouro.optional")
    )
    with runtime.snapshot() as snapshot:
        assert "dev.ouro.optional/tools/maybe" not in snapshot.generations[0].contract_hashes
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
async def test_failed_digest_selection_never_replaces_previous_active_generation(tmp_path) -> None:
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
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
        contributes=contributes,
    )
    _write_native_plugin(
        second_source,
        version="2.0.0",
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
        contributes=contributes,
    )
    (first_source / "echo_plugin.py").write_text(
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
    (second_source / "echo_plugin.py").write_text(
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
    with runtime.snapshot() as before:
        previous_generation_id = before.generations[0].generation_id

    with pytest.raises(RuntimeError, match="candidate failed"):
        await service.select("dev.ouro.echo", second.package_digest)

    assert service.show("dev.ouro.echo").package_digest == first.package_digest
    with runtime.snapshot() as snapshot:
        assert snapshot.generations[0].generation_id == previous_generation_id
        result = await snapshot.tools.get("rollback__run").execute(
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
async def test_failed_active_configuration_never_commits_candidate_revision(tmp_path) -> None:
    source = tmp_path / "configuration-rollback"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.configuration",
        runtime={"kind": "python-inprocess", "entrypoint": "configuration_plugin:create_plugin"},
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
    (source / "configuration_plugin.py").write_text(
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
    with runtime.snapshot() as before:
        previous_generation_id = before.generations[0].generation_id

    with pytest.raises(RuntimeError, match="new configuration failed"):
        await service.configure("dev.ouro.configuration", {"fail": True})

    record = service.store.read_state().plugins["dev.ouro.configuration"]
    selected = record.selected()
    assert selected is not None
    active_configuration = record.activation.configuration_for(selected.package_digest)
    assert active_configuration is not None
    assert active_configuration.values == {"fail": False}
    assert record.activation.active_configuration_revision_id == active_configuration.revision_id
    assert len(record.activation.configuration_revisions) == 2
    assert service.show("dev.ouro.configuration").status == "active"
    with runtime.snapshot() as current:
        assert current.generations[0].generation_id == previous_generation_id
    await runtime.close()


@pytest.mark.asyncio
async def test_sdk_tool_id_must_match_manifest_even_when_exposed_name_matches(tmp_path) -> None:
    source = tmp_path / "tool-identity"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.tool_identity",
        runtime={"kind": "python-inprocess", "entrypoint": "tool_identity_plugin:create_plugin"},
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
    (source / "tool_identity_plugin.py").write_text(
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
async def test_sdk_tool_permission_contract_must_match_manifest(tmp_path) -> None:
    source = tmp_path / "tool-permission"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.tool_permission",
        runtime={"kind": "python-inprocess", "entrypoint": "tool_permission_plugin:create_plugin"},
        contributes={
            "tools": [
                {
                    "id": "control",
                    "exposed_name": "permission__control",
                    "description": "Control the harness.",
                    "permission_mode": "auto",
                    "permission_control": "tool",
                }
            ]
        },
    )
    (source / "tool_permission_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet, ToolContribution
class Plugin:
    async def prepare(self):
        async def control(call): return 'bad'
        return ContributionSet(tools=[ToolContribution(
            id='control', exposed_name='permission__control',
            description='Control the harness.', permission_mode='approval',
            handler=control,
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
    service.authorize("dev.ouro.tool_permission")
    service.trust_code("dev.ouro.tool_permission")

    assert (await service.enable("dev.ouro.tool_permission")).status == "failed"
    assert any(
        "tool contract does not match manifest" in item.message
        for item in service.diagnostics("dev.ouro.tool_permission")
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_sdk_tool_description_must_match_manifest(tmp_path) -> None:
    source = tmp_path / "tool-description"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.tool_description",
        runtime={"kind": "python-inprocess", "entrypoint": "tool_description_plugin:create_plugin"},
        contributes={
            "tools": [
                {
                    "id": "inspect",
                    "exposed_name": "description__inspect",
                    "description": "Use this tool to inspect the selected resource.",
                }
            ]
        },
    )
    (source / "tool_description_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet, ToolContribution
class Plugin:
    async def prepare(self):
        async def inspect(call): return 'bad'
        return ContributionSet(tools=[ToolContribution(
            id='inspect', exposed_name='description__inspect',
            description='Inspect a resource.', handler=inspect,
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
    service.authorize("dev.ouro.tool_description")
    service.trust_code("dev.ouro.tool_description")

    assert (await service.enable("dev.ouro.tool_description")).status == "failed"
    assert any(
        "tool description does not match manifest" in item.message
        for item in service.diagnostics("dev.ouro.tool_description")
    )
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
async def test_builtin_contract_participates_in_dependency_lock(tmp_path) -> None:
    compatible = tmp_path / "compatible"
    incompatible = tmp_path / "incompatible"
    compatible.mkdir()
    incompatible.mkdir()
    _write_native_plugin(
        compatible,
        plugin_id="dev.ouro.compatible",
        dependencies=[{"plugin_id": "dev.ouro.builtin", "version": "^2.0.0"}],
    )
    _write_native_plugin(
        incompatible,
        plugin_id="dev.ouro.incompatible",
        dependencies=[{"plugin_id": "dev.ouro.builtin", "version": "^3.0.0"}],
    )
    definition = _builtin_plugin(
        tmp_path / "builtin",
        plugin_id="dev.ouro.builtin",
        version="2.1.0",
    )
    builtin_plugins = BuiltinPluginRegistry((definition,))
    contracts = builtin_plugins.contracts
    runtime, *_ = _runtime(tmp_path / "home", builtin_plugins=builtin_plugins)
    service = PluginService(
        runtime.store,
        runtime=runtime,
        builtin_plugins=builtin_plugins,
    )
    service.install(compatible)
    service.install(incompatible)

    assert (await service.enable("dev.ouro.compatible")).status == "active"
    graph = resolve_plugin_dependencies(
        runtime.store.read_state(),
        roots={"dev.ouro.compatible"},
        builtin_contracts=contracts,
    )
    builtin = graph.nodes[0]
    assert builtin.plugin_class == "builtin"
    assert builtin.package_digest is None
    assert builtin.contract_hash == definition.contract_hash
    with runtime.snapshot() as catalog:
        assert [item.plugin_id for item in catalog.generations] == [
            "dev.ouro.builtin",
            "dev.ouro.compatible",
        ]

    assert (await service.enable("dev.ouro.incompatible")).status == "incompatible"
    assert service.show("dev.ouro.incompatible").status == "incompatible"
    assert any(
        item.code == "plugin_dependency_resolution_failed"
        and "builtin dev.ouro.builtin 2.1.0 does not satisfy ^3.0.0" in item.message
        for item in service.diagnostics("dev.ouro.incompatible")
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_builtin_plugin_uses_generation_lifecycle_and_persisted_override(tmp_path) -> None:
    home = tmp_path / "home"
    definition = _builtin_plugin(
        tmp_path / "builtin",
        plugin_id="dev.ouro.builtin",
        default_enabled=True,
    )
    builtin_plugins = BuiltinPluginRegistry((definition,))
    runtime, *_ = _runtime(home, builtin_plugins=builtin_plugins)
    service = PluginService(
        runtime.store,
        runtime=runtime,
        builtin_plugins=builtin_plugins,
    )

    await runtime.start()

    detail = service.show("dev.ouro.builtin")
    assert detail.plugin_class == "builtin"
    assert detail.default_enabled is True
    assert detail.desired_enabled is True
    assert detail.status == "active"
    assert detail.package_digest is None
    assert detail.builtin_contract_hash == definition.contract_hash
    assert detail.installs == []
    assert runtime.store.read_state().builtin_activation_overrides == {}
    with runtime.snapshot() as snapshot:
        generation = snapshot.generations[0]
        assert generation.plugin_class == "builtin"
        assert generation.release_id == definition.contract_hash
        assert generation.builtin_contract_hash == definition.contract_hash
        assert generation.package_digest is None

    assert (await service.disable("dev.ouro.builtin")).status == "disabled"
    override = runtime.store.read_state().builtin_activation_overrides["dev.ouro.builtin"]
    assert override.desired_enabled is False
    with pytest.raises(PluginOperationNotAllowedError, match="cannot remove builtin plugin"):
        await service.remove("dev.ouro.builtin")
    with pytest.raises(PluginOperationNotAllowedError, match="cannot configure builtin plugin"):
        await service.configure("dev.ouro.builtin", {})
    await runtime.close()

    restarted, *_ = _runtime(home, builtin_plugins=builtin_plugins)
    restarted_service = PluginService(
        restarted.store,
        runtime=restarted,
        builtin_plugins=builtin_plugins,
    )
    await restarted.start()
    assert restarted_service.show("dev.ouro.builtin").status == "disabled"
    assert (await restarted_service.enable("dev.ouro.builtin")).status == "active"
    await restarted.close()


@pytest.mark.asyncio
async def test_catalog_leases_replace_lifecycle_blocks_and_enable_is_idempotent(
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
    service.install(first_source)
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

    assert (await service.restart("dev.ouro.echo")).status == "active"
    assert (await service.select("dev.ouro.echo", second.package_digest)).status == "active"
    assert (await service.configure("dev.ouro.echo", {})).status == "active"
    assert service.show("dev.ouro.echo").package_digest == second.package_digest
    await service.disable("dev.ouro.echo")
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_candidate_discards_staged_data_and_desired_state(tmp_path) -> None:
    source = tmp_path / "staged-candidate"
    source.mkdir()
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
    )
    (source / "echo_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import ContributionSet
class Plugin:
    def __init__(self, context): self.context = context
    async def prepare(self): return ContributionSet()
    async def start(self):
        self.context.data.write_bytes('candidate.txt', b'must-not-commit')
        raise RuntimeError('candidate rejected')
    async def stop(self, reason, deadline):
        self.context.data.write_bytes('candidate.txt', reason.encode())
def create_plugin(context): return Plugin(context)
""",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    runtime, *_ = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.trust_code("dev.ouro.echo")
    expected = runtime.store.read_state()

    assert (await service.enable("dev.ouro.echo")).status == "failed"
    observed = runtime.store.read_state()
    assert observed.plugins["dev.ouro.echo"].activation.desired_enabled is True
    expected.plugins["dev.ouro.echo"].activation.desired_enabled = True
    assert observed == expected
    assert not (home / "plugins" / "data" / "dev.ouro.echo" / "candidate.txt").exists()
    with runtime.snapshot() as catalog:
        assert catalog.generations == ()
    await runtime.close()


@pytest.mark.asyncio
async def test_configuration_rebuilds_transitive_desired_dependants(tmp_path) -> None:
    root = tmp_path / "root"
    middle = tmp_path / "middle"
    leaf = tmp_path / "leaf"
    root.mkdir()
    middle.mkdir()
    leaf.mkdir()
    _write_native_plugin(
        root,
        plugin_id="dev.ouro.root",
        extra={"configuration": {"schema": "./configuration.schema.json", "version": 1}},
    )
    (root / "configuration.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "required": ["mode"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    _write_native_plugin(
        middle,
        plugin_id="dev.ouro.middle",
        dependencies=[{"plugin_id": "dev.ouro.root", "version": "^1.0.0"}],
    )
    _write_native_plugin(
        leaf,
        plugin_id="dev.ouro.leaf",
        dependencies=[{"plugin_id": "dev.ouro.middle", "version": "^1.0.0"}],
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(root)
    service.install(middle)
    service.install(leaf)
    await service.configure("dev.ouro.root", {"mode": "old"})
    assert (await service.enable("dev.ouro.leaf")).status == "active"
    with runtime.snapshot() as before:
        old_generations = {
            generation.plugin_id: generation.generation_id for generation in before.generations
        }
        old_execution_contracts = {
            generation.plugin_id: generation.execution_contract_hash
            for generation in before.generations
        }
        old_dependency_locks = {
            generation.plugin_id: generation.dependency_lock_fingerprint
            for generation in before.generations
        }

    assert (await service.configure("dev.ouro.root", {"mode": "new"})).status == "active"
    with runtime.snapshot() as after:
        new_generations = {
            generation.plugin_id: generation.generation_id for generation in after.generations
        }
        new_execution_contracts = {
            generation.plugin_id: generation.execution_contract_hash
            for generation in after.generations
        }
        new_dependency_locks = {
            generation.plugin_id: generation.dependency_lock_fingerprint
            for generation in after.generations
        }
    assert set(new_generations) == {
        "dev.ouro.root",
        "dev.ouro.middle",
        "dev.ouro.leaf",
    }
    assert all(new_generations[item] != old_generations[item] for item in new_generations)
    assert all(
        new_execution_contracts[item] != old_execution_contracts[item]
        for item in new_execution_contracts
    )
    assert new_dependency_locks["dev.ouro.root"] == old_dependency_locks["dev.ouro.root"]
    assert new_dependency_locks["dev.ouro.middle"] != old_dependency_locks["dev.ouro.middle"]
    assert new_dependency_locks["dev.ouro.leaf"] != old_dependency_locks["dev.ouro.leaf"]
    await runtime.close()


@pytest.mark.asyncio
async def test_background_service_ownership_handover_is_ordered(tmp_path) -> None:
    source = tmp_path / "service-handover"
    source.mkdir()
    _write_native_plugin(
        source,
        plugin_id="dev.ouro.service_handover",
        runtime={
            "kind": "python-inprocess",
            "entrypoint": "service_handover_plugin:create_plugin",
        },
        contributes={"background_services": [{"id": "watcher"}]},
        extra={"configuration": {"schema": "./configuration.schema.json", "version": 1}},
    )
    (source / "configuration.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "required": ["mode"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    (source / "service_handover_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import BackgroundServiceContribution, ContributionSet
class Plugin:
    def __init__(self, context): self.context = context
    def append(self, value):
        try: current = self.context.data.read_bytes('events.txt')
        except FileNotFoundError: current = b''
        self.context.data.write_bytes('events.txt', current + value.encode() + b'\\n')
    async def prepare(self):
        mode = str(self.context.configuration['mode'])
        return ContributionSet(background_services=[BackgroundServiceContribution(
            id='watcher',
            start=lambda: self.append('start:' + mode),
            stop=lambda: self.append('stop:' + mode),
        )])
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin(context)
""",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    runtime, *_ = _runtime(home)
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    service.authorize("dev.ouro.service_handover")
    service.trust_code("dev.ouro.service_handover")
    await service.configure("dev.ouro.service_handover", {"mode": "old"})
    assert (await service.enable("dev.ouro.service_handover")).status == "active"
    await service.configure("dev.ouro.service_handover", {"mode": "new"})

    events = (
        (home / "plugins" / "data" / "dev.ouro.service_handover" / "events.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert events == ["start:old", "stop:old", "start:new"]
    await runtime.close()


@pytest.mark.asyncio
async def test_candidate_state_commit_detects_concurrent_mutation(tmp_path, monkeypatch) -> None:
    source = tmp_path / "state-conflict"
    source.mkdir()
    _write_native_plugin(source)
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    commit = runtime.store.commit_candidate_state
    injected = False

    def conflict(expected, candidate):
        nonlocal injected
        if not injected:
            injected = True

            def mutate(state):
                state.plugins["dev.ouro.echo"].activation.desired_enabled = True
                return state

            runtime.store.mutate_state(mutate)
        return commit(expected, candidate)

    monkeypatch.setattr(runtime.store, "commit_candidate_state", conflict)
    assert (await service.enable("dev.ouro.echo")).status == "failed"
    with runtime.snapshot() as catalog:
        assert catalog.generations == ()
    assert any(
        "state changed while an activation candidate was building" in item.message
        for item in service.diagnostics("dev.ouro.echo")
    )
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


def test_native_manifest_model_ignores_unknown_fields() -> None:
    manifest = PluginManifest.model_validate(
        {
            "schema_version": 3,
            "plugin_id": "dev.ouro.strict",
            "name": "Strict",
            "version": "1.0.0",
            "namespace": "strict",
            "engines": {"ouro": "^0.1.0", "plugin_api": "^2.0.0"},
            "verified": True,
        }
    )

    assert "verified" not in manifest.model_dump()


def test_native_manifest_rejects_tool_controlled_approval_permission() -> None:
    with pytest.raises(ValueError, match="tool-controlled permission must be auto or deny"):
        PluginManifest.model_validate(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.control",
                "name": "Control",
                "version": "1.0.0",
                "namespace": "control",
                "engines": {"ouro": "^0.1.0", "plugin_api": "^2.0.0"},
                "contributes": {
                    "tools": [
                        {
                            "id": "control",
                            "exposed_name": "control__harness",
                            "description": "Control the harness.",
                            "permission_mode": "approval",
                            "permission_control": "tool",
                        }
                    ]
                },
            }
        )


def test_native_manifest_rejects_unknown_activation_binding() -> None:
    with pytest.raises(ValueError, match="unknown session controller"):
        PluginManifest.model_validate(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.bound",
                "name": "Bound",
                "version": "1.0.0",
                "namespace": "bound",
                "engines": {"ouro": "^0.1.0", "plugin_api": "^2.0.0"},
                "contributes": {"context_providers": [{"id": "context", "active_when": "missing"}]},
            }
        )


def test_native_manifest_rejects_unknown_interaction_controller() -> None:
    with pytest.raises(ValueError, match="unknown session controller"):
        PluginManifest.model_validate(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.bound",
                "name": "Bound",
                "version": "1.0.0",
                "namespace": "bound",
                "engines": {"ouro": "^0.1.0", "plugin_api": "^2.0.0"},
                "contributes": {
                    "tools": [
                        {
                            "id": "finish",
                            "exposed_name": "FinishMode",
                            "description": "Finish the active mode.",
                            "resumable": True,
                            "interaction_controller": "missing",
                        }
                    ]
                },
            }
        )


@pytest.mark.asyncio
async def test_plugin_command_boundary_is_namespaced_timed_and_redacted() -> None:
    registry = PluginCommandRegistry()

    async def failing_command(_request):
        raise RuntimeError("token=command-secret")

    registry.register(
        PluginCommand(
            name="fail",
            plugin_id="example.commands",
            handler=failing_command,
            timeout_seconds=0.1,
        )
    )

    with pytest.raises(PluginCommandError) as raised:
        await registry.execute("example.commands:fail")

    assert "command-secret" not in str(raised.value)
    assert "token=***" in str(raised.value)


@pytest.mark.asyncio
async def test_plugin_command_attributes_an_atomic_input_batch_as_one_owner() -> None:
    registry = PluginCommandRegistry()

    async def batch_command(request):
        return PluginCommandResult(
            command_id=request.command_id,
            session_input_batches=[
                SessionInputBatchRequest(
                    inputs=(
                        SessionInputRequest(
                            prompt="control",
                            role="user",
                            semantic_tag="goal-updated",
                        ),
                        SessionInputRequest(prompt="objective", role="user"),
                    ),
                    coalesce_key="goal-lifecycle",
                )
            ],
        )

    registry.register(
        PluginCommand(
            name="batch",
            plugin_id="dev.example.commands",
            handler=batch_command,
            allow_session_inputs=True,
            generation_id="generation_1",
            execution_contract_hash="contract_1",
        )
    )

    result = await registry.execute("dev.example.commands:batch")

    assert result.attributed_session_inputs == []
    assert len(result.attributed_session_input_batches) == 1
    batch = result.attributed_session_input_batches[0]
    assert batch.coalesce_key == "goal-lifecycle"
    assert [item.prompt for item in batch.inputs] == ["control", "objective"]
    assert {item.plugin_id for item in batch.inputs} == {"dev.example.commands"}
    assert {item.contribution_id for item in batch.inputs} == {"batch"}
