from __future__ import annotations

import weakref
from dataclasses import replace

import pytest

from ouro.core.observers import ObserverRegistry
from ouro.plugins.builtin import BuiltinPluginDefinition, BuiltinPluginRegistry
from ouro.plugins.catalog.gates import PluginGenerationUnavailableError
from ouro.plugins.contributions.preparation import ContributionCoordinator
from ouro.plugins.installation.store import PluginPackageStore
from ouro.plugins.invocation import plugin_invocation
from ouro.plugins.lifecycle.runtime import PluginRuntimeResource
from ouro.plugins.manifest import PluginManifest
from ouro.sdk.plugins import v5 as sdk
from ouro.tools import Tool, ToolDefinition, ToolInvocation, ToolRegistry
from ouro.tools.executor import ToolExecutor

PLUGIN = "dev.ouro.discovery"


def group(revision="one", *, name="discovered", argument="query"):
    return sdk.ToolGroupContribution(
        "remote",
        revision,
        (
            sdk.ToolContribution(
                "search",
                name,
                "Search a discovered source",
                lambda call: f"{revision}:{call.arguments[argument]}",
                parameters_schema={
                    "type": "object",
                    "properties": {argument: {"type": "string"}},
                    "required": [argument],
                    "additionalProperties": False,
                },
                permission_mode="auto",
                effect="read_only",
            ),
        ),
    )


class Discovery:
    def __init__(self, context, instances):
        self.context = context
        instances.append(self)

    async def prepare(self):
        return sdk.ContributionSet(tool_groups=[group()])

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def make_runtime(tmp_path, instances, factory=Discovery, *, contributes=None):
    manifest = PluginManifest.model_validate(
        {
            "plugin_id": PLUGIN,
            "namespace": "discovery",
            "name": "Discovery",
            "version": "1.0.0",
            "engines": {"ouro": "^0.3.0", "plugin_api": "^5.0.0"},
            "runtime": {"kind": "python-inprocess", "entrypoint": "discovery_plugin:create"},
            "tool_groups": [{"id": "remote", "permission_mode": "auto", "effect": "read_only"}],
            "contributes": contributes or {},
        }
    )
    return PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=ToolRegistry(), observers=ObserverRegistry()),
        builtin_plugins=BuiltinPluginRegistry(
            (
                BuiltinPluginDefinition(
                    manifest, lambda ctx: factory(ctx, instances), tmp_path, default_enabled=False
                ),
            )
        ),
    )


@pytest.fixture
async def discovery(tmp_path):
    instances = []
    runtime = make_runtime(tmp_path, instances)
    try:
        assert await runtime.enable(PLUGIN) == "active", runtime.diagnostics()
        yield runtime, instances
    finally:
        await runtime.close()


def invocation(catalog, *, name="discovered", arguments=None):
    definition = catalog.tools.get(name).definition
    metadata = definition.metadata
    return ToolInvocation(
        session_id="session",
        run_id="run",
        tool_name=name,
        arguments=arguments or {"query": "term"},
        canonical_tool_id=f"{PLUGIN}/tools/{metadata['contribution_id']}",
        source=definition.source,
        **{
            field: metadata[field]
            for field in (
                "plugin_id",
                "plugin_class",
                "release_id",
                "builtin_contract_hash",
                "contract_hash",
                "execution_contract_hash",
            )
        },
    )


async def test_replacement_preserves_accepted_schema_handler_and_generation_contract(discovery):
    runtime, instances = discovery
    ports = instances[-1].context.tool_groups
    with runtime.snapshot() as accepted:
        original = invocation(accepted)
        handle = await ports.publish(group("two", argument="topic"))
        with runtime.snapshot() as current:
            updated = invocation(current, arguments={"topic": "new"})
            assert updated.execution_contract_hash == original.execution_contract_hash
            assert updated.contract_hash != original.contract_hash
            assert (
                "query"
                in accepted.tools.get("discovered").definition.parameters_schema["properties"]
            )
            old_result = await ToolExecutor(accepted.tools).execute(original)
            new_result = await ToolExecutor(current.tools).execute(updated)
            assert old_result.result.content == "one:term"
            assert new_result.result.content == "two:new"
            mismatch = ToolExecutor(current.tools).preflight(original)
            assert mismatch.result.structured["error_type"] == "ToolExecutionContractMismatch"
        await handle.close()
        await handle.close()
        with runtime.snapshot() as withdrawn:
            assert not withdrawn.tools.definitions()
        assert (await ToolExecutor(accepted.tools).execute(original)).result.content == "one:term"


async def test_closing_replaced_registration_cannot_withdraw_current_group(discovery):
    runtime, instances = discovery
    ports = instances[-1].context.tool_groups
    old = await ports.publish(group("two"))
    current = await ports.publish(group("three"))
    await old.close()
    with runtime.snapshot() as catalog:
        result = await ToolExecutor(catalog.tools).execute(invocation(catalog))
        assert result.result.content == "three:term"
    await current.close()
    with runtime.snapshot() as catalog:
        assert not catalog.tools.definitions()


@pytest.mark.parametrize(
    "failure", ["conflict", "schema", "policy", "identity", "group", "revision"]
)
async def test_invalid_publication_keeps_entire_previous_group(discovery, failure):
    runtime, instances = discovery
    runtime.coordinator.tools.register(
        Tool(
            definition=ToolDefinition(name="host_tool", description="Host"),
            handler=lambda *_: "host",
        )
    )
    invalid = group("two")
    tool = invalid.tools[0]
    if failure == "conflict":
        tool = replace(tool, exposed_name="host_tool")
    elif failure == "schema":
        tool = replace(tool, parameters_schema={"type": "not-json-schema"})
    elif failure == "policy":
        tool = replace(tool, effect="state_changing")
    elif failure == "identity":
        invalid = replace(invalid, tools=(tool, replace(tool, exposed_name="other")))
    elif failure == "group":
        invalid = replace(invalid, id="undeclared")
    elif failure == "revision":
        invalid = replace(invalid, revision=" ")
    if failure in {"conflict", "schema", "policy"}:
        invalid = replace(invalid, tools=(tool,))
    with runtime.snapshot() as before:
        original = invocation(before)
        before_id = before.catalog_id
    with pytest.raises(ValueError):
        await instances[-1].context.tool_groups.publish(invalid)
    with runtime.snapshot() as after:
        assert after.catalog_id == before_id
        assert invocation(after).contract_hash == original.contract_hash
        assert {item.name for item in after.tools.definitions()} == {"discovered", "host_tool"}


async def test_rebase_keeps_original_group_and_retired_unleased_catalog_cannot_reopen(discovery):
    runtime, instances = discovery
    accepted = runtime.snapshot()
    old_catalog = accepted.catalog
    try:
        await instances[-1].context.tool_groups.publish(group("two"))
        runtime.coordinator.tools.register(
            Tool(
                definition=ToolDefinition(name="host_tool", description="Host"),
                handler=lambda *_: "host",
            )
        )
        with runtime.snapshot() as current:
            assert current.tools.get("host_tool")
            assert (
                await ToolExecutor(current.tools).execute(invocation(current))
            ).result.content == "two:term"
        rebased = old_catalog.rebase(runtime._catalog_base)
        with rebased.acquire() as old:
            assert (
                await ToolExecutor(old.tools).execute(invocation(old))
            ).result.content == "one:term"
    finally:
        accepted.close()
    with pytest.raises(RuntimeError, match="publication is unavailable"):
        old_catalog.acquire()
    assert runtime._active[PLUGIN].lease_count == 0


async def test_read_only_expired_and_retired_scopes_cannot_publish(discovery):
    runtime, instances = discovery
    ports = instances[-1].context.tool_groups
    with plugin_invocation(PLUGIN, writable=False):
        with pytest.raises(PermissionError, match="read-only"):
            await ports.publish(group("two"))
    with plugin_invocation(PLUGIN, writable=True) as scope:
        scope.close()
        with pytest.raises(RuntimeError, match="finished"):
            await ports.publish(group("two"))
    with runtime.snapshot() as accepted:
        await runtime.restart(PLUGIN)
        with pytest.raises(RuntimeError, match="current generation"):
            await ports.publish(group("two"))
        result = await ToolExecutor(accepted.tools).execute(invocation(accepted))
        assert result.result.content == "one:term"
        await runtime.disable(PLUGIN)
        with pytest.raises(PluginGenerationUnavailableError):
            await accepted.tools.get("discovered").execute(invocation(accepted))


async def test_lifecycle_mutation_rejects_publication_without_waiting(discovery):
    runtime, instances = discovery
    async with runtime._mutation_lock:
        with pytest.raises(RuntimeError, match="lifecycle mutation"):
            await instances[-1].context.tool_groups.publish(group("two"))
    with runtime.snapshot() as accepted:
        assert (
            await ToolExecutor(accepted.tools).execute(invocation(accepted))
        ).result.content == "one:term"


@pytest.mark.parametrize("phase", ["prepare", "start"])
async def test_candidate_cannot_publish_outside_prepared_contributions(tmp_path, phase):
    class Candidate(Discovery):
        async def prepare(self):
            if phase == "prepare":
                await self.context.tool_groups.publish(group())
            return await super().prepare()

        async def start(self):
            if phase == "start":
                await self.context.tool_groups.publish(group())

    runtime = make_runtime(tmp_path, [], Candidate)
    try:
        assert await runtime.enable(PLUGIN) == "failed"
        assert any("active plugin generation" in item.message for item in runtime.diagnostics())
        with runtime.snapshot() as catalog:
            assert not catalog.tools.definitions()
    finally:
        await runtime.close()


async def test_restart_matches_revision_and_rejects_changed_implementation(discovery):
    runtime, instances = discovery
    with runtime.snapshot() as catalog:
        original = invocation(catalog)
    await runtime.restart(PLUGIN)
    with runtime.snapshot() as catalog:
        result = await ToolExecutor(catalog.tools).execute(original)
        assert result.result.content == "one:term"
    await instances[-1].context.tool_groups.publish(group("two"))
    with runtime.snapshot() as catalog:
        result = await ToolExecutor(catalog.tools).execute(original)
        assert result.result.structured["error_type"] == "ToolExecutionContractMismatch"


async def test_published_schema_is_detached_from_plugin_mutation(discovery):
    runtime, instances = discovery
    supplied = group("two")
    await instances[-1].context.tool_groups.publish(supplied)
    supplied.tools[0].parameters_schema["properties"]["query"]["type"] = "integer"
    with runtime.snapshot() as catalog:
        schema = catalog.tools.get("discovered").definition.parameters_schema
        assert schema["properties"]["query"]["type"] == "string"


async def test_plugin_stop_can_close_owned_tool_group_registration(tmp_path):
    class ClosingDiscovery(Discovery):
        handle = None

        async def stop(self, reason, deadline):
            if self.handle is not None:
                await self.handle.close()

    instances = []
    runtime = make_runtime(tmp_path, instances, ClosingDiscovery)
    try:
        assert await runtime.enable(PLUGIN) == "active"
        instances[0].handle = await instances[0].context.tool_groups.publish(group("two"))
        assert await runtime.disable(PLUGIN) == "disabled", runtime.diagnostics()
        assert not runtime.has_resources(PLUGIN)
        assert not runtime.diagnostics()
    finally:
        instances[0].handle = None
        await runtime.close()


async def test_group_replacement_removes_missing_tools_and_preserves_static_contributions(tmp_path):
    static = group("static", name="static").tools[0]

    class MixedDiscovery(Discovery):
        async def prepare(self):
            dynamic = group()
            return sdk.ContributionSet(
                tools=[static],
                tool_groups=[
                    replace(
                        dynamic,
                        tools=(
                            *dynamic.tools,
                            replace(dynamic.tools[0], id="other", exposed_name="other"),
                        ),
                    )
                ],
            )

    instances = []
    runtime = make_runtime(
        tmp_path,
        instances,
        MixedDiscovery,
        contributes={
            "tools": [
                {
                    "id": static.id,
                    "exposed_name": static.exposed_name,
                    "description": static.description,
                    "permission_mode": "auto",
                    "effect": "read_only",
                }
            ]
        },
    )
    try:
        assert await runtime.enable(PLUGIN) == "active", runtime.diagnostics()
        with runtime.snapshot() as catalog:
            assert catalog.tools.names() == ["discovered", "other", "static"]
        await instances[0].context.tool_groups.publish(group("two"))
        with runtime.snapshot() as catalog:
            assert catalog.tools.names() == ["discovered", "static"]
            assert catalog.contract_hash(f"{PLUGIN}/tools/search") is not None
            assert catalog.contract_hash(f"{PLUGIN}/tools/remote/search") is not None
            assert catalog.contract_hash(f"{PLUGIN}/tools/remote/other") is None
        await instances[0].context.tool_groups.publish(sdk.ToolGroupContribution("remote", "empty"))
        with runtime.snapshot() as catalog:
            assert catalog.tools.names() == ["static"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("invalid", ["duplicate", "declarative", "controller", "unattended"])
def test_manifest_tool_group_policy_is_validated_before_activation(tmp_path, invalid):
    runtime = make_runtime(tmp_path, [])
    manifest = runtime.builtin_plugins.require(PLUGIN).manifest
    assert manifest.capabilities() == {"tools"}
    raw = manifest.model_dump(mode="json")
    if invalid == "duplicate":
        raw["tool_groups"].append(raw["tool_groups"][0].copy())
    elif invalid == "declarative":
        raw["runtime"] = {"kind": "declarative"}
    elif invalid == "controller":
        raw["tool_groups"][0]["active_when"] = "missing"
    elif invalid == "unattended":
        raw["tool_groups"][0]["supports_unattended"] = True
    with pytest.raises(ValueError):
        PluginManifest.model_validate(raw)


async def test_obsolete_registration_handle_does_not_retain_unleased_handlers(discovery):
    _, instances = discovery
    ports = instances[-1].context.tool_groups

    class Handler:
        def __call__(self, call):
            return "owned"

    handler = Handler()
    reference = weakref.ref(handler)
    candidate = group("owned")
    candidate = replace(candidate, tools=(replace(candidate.tools[0], handler=handler),))
    handle = await ports.publish(candidate)
    del candidate, handler
    await ports.publish(group("replacement"))
    assert reference() is None
    await handle.close()
