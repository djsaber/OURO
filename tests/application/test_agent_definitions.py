from __future__ import annotations

import pytest

from ouro.application.agents.definitions import AgentDefinitionService
from ouro.application.configuration.facade import ConfigurationFacade
from ouro.application.runtime.service import RuntimeService
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentSpawnPolicy, ToolSelection
from ouro.core.errors import ConfigError
from ouro.core.model_selection import DEFAULT_MODEL
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


async def _handler(invocation, context):
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content="ok",
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Read",
                description="Read data.",
                effect="read_only",
            ),
            handler=_handler,
        )
    )
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="Write",
                description="Write data.",
                effect="state_changing",
            ),
            handler=_handler,
        )
    )
    return registry


def test_general_and_custom_definitions_share_one_catalog_pipeline(tmp_path) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    created = service.create(
        name="Reader",
        description="Read-only investigation.",
        system_prompt="Inspect the requested evidence.",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=_registry(),
        expected_revision=0,
    )
    reader_id = created.definitions[-1].agent_id

    assert [item.agent_id for item in created.definitions] == ["general", reader_id]
    assert created.definitions[0].editable is False
    assert created.definitions[1].editable is True
    assert "system_prompt" not in created.model_dump_json()
    assert service.get(reader_id, tools=_registry()).system_prompt == (
        "Inspect the requested evidence."
    )

    snapshot = service.catalog_snapshot(_registry())
    assert [entry.definition.agent_id for entry in snapshot.entries] == ["general", reader_id]
    assert {item.public_name for item in snapshot.resolve("general").effective_tools} == {
        "Read",
        "Write",
    }
    assert [item.public_name for item in snapshot.resolve(reader_id).effective_tools] == ["Read"]


def test_default_subagent_model_requires_explicit_system_route(tmp_path) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    options = dict(
        name="Reader",
        description="Read evidence.",
        system_prompt="Read carefully.",
        tool_selection=ToolSelection(mode="all"),
        model=DEFAULT_MODEL,
        tools=_registry(),
        expected_revision=0,
    )
    with pytest.raises(ConfigError, match="system default endpoint is not configured"):
        service.create(**options)

    ConfigurationFacade(tmp_path).add_provider(
        display_name="Primary", provider_type="openai", make_default=True
    )
    catalog = service.create(**options)
    assert catalog.definitions[1].model == DEFAULT_MODEL
    assert service.default_model_route().count("/") == 1


def test_missing_selected_tool_keeps_definition_but_excludes_it_from_resolution(tmp_path) -> None:
    manager = AgentDefinitionsConfigManager(tmp_path)
    service = AgentDefinitionService(manager)
    created = service.create(
        name="Reader",
        description="Read-only investigation.",
        system_prompt="Inspect evidence.",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=_registry(),
        expected_revision=0,
    )
    reader_id = created.definitions[-1].agent_id
    empty_registry = ToolRegistry()

    catalog = service.list(tools=empty_registry)
    reader = next(item for item in catalog.definitions if item.agent_id == reader_id)
    assert reader.compatibility == "unavailable"
    assert reader.missing_tool_names == ("Read",)
    with pytest.raises(KeyError, match="unknown or unavailable"):
        service.catalog_snapshot(empty_registry).resolve(reader_id)


def test_definition_model_can_be_set_and_cleared_atomically(tmp_path) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    created = service.create(
        name="Reader",
        description="Read evidence.",
        system_prompt="Read carefully.",
        tool_selection=ToolSelection(mode="all"),
        model="faux/reviewer",
        tools=_registry(),
        expected_revision=0,
    )
    reader_id = created.definitions[-1].agent_id

    assert service.get(reader_id, tools=_registry()).model == "faux/reviewer"

    updated = service.update(
        agent_id=reader_id,
        name="Reader",
        description="Read evidence.",
        system_prompt="Read carefully.",
        tool_selection=ToolSelection(mode="all"),
        model=None,
        tools=_registry(),
        expected_revision=created.revision,
    )

    assert next(item for item in updated.definitions if item.agent_id == reader_id).model is None


@pytest.mark.parametrize("selection_mode", ["all", "selected"])
def test_catalog_excludes_hard_denied_tools_and_freezes_approval_floor(
    tmp_path,
    selection_mode,
) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    if selection_mode == "selected":
        created = service.create(
            name="Reader",
            description="Read-only investigation.",
            system_prompt="Inspect evidence.",
            tool_selection=ToolSelection(mode="selected", names=("Read", "Write")),
            tools=_registry(),
            expected_revision=0,
        )
        reader_id = created.definitions[-1].agent_id
        agent_id = reader_id
    else:
        agent_id = "general"

    catalog = service.catalog_snapshot(
        _registry(),
        permission_overrides={"Read": "approval", "Write": "deny"},
        approval_mode="inherit",
    )

    refs = catalog.resolve(agent_id).effective_tools
    assert [ref.public_name for ref in refs] == ["Read"]
    assert refs[0].minimum_permission_mode == "approval"


@pytest.mark.parametrize("source", ["builtin", "third_party_plugin", "mcp"])
@pytest.mark.parametrize("effect", ["read_only", "state_changing", "unknown"])
def test_agent_tool_filters_use_declared_effect_and_source(tmp_path, source, effect):
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="inspect",
                description="Inspect data.",
                source=source,
                effect=effect,
            ),
            handler=_handler,
        )
    )
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    reference = service.catalog_snapshot(registry).resolve("general").effective_tools[0]
    assert AgentSpawnPolicy().allows_tool(reference)
    assert AgentSpawnPolicy(allow_write=False).allows_tool(reference) == (effect == "read_only")
    assert AgentSpawnPolicy(allow_mcp=False).allows_tool(reference) == (source != "mcp")


def test_builtin_definition_is_immutable_but_can_be_copied(tmp_path) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))

    with pytest.raises(ConfigError, match="immutable"):
        service.delete("general", expected_revision=0)
    copied = service.copy_builtin("general", name="Custom", tools=_registry(), expected_revision=0)
    custom_id = copied.definitions[-1].agent_id

    custom = next(item for item in copied.definitions if item.agent_id == custom_id)
    assert custom.editable is True
    assert custom.name == "Custom"


@pytest.mark.asyncio
async def test_runtime_persists_run_frozen_agent_catalog(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")),
        tools=_registry(),
    )
    runtime.bind_extensions(agent_definitions=service)

    first = await runtime.run("first")
    assert first.run.agent_catalog is not None
    assert [entry.definition.agent_id for entry in first.run.agent_catalog.entries] == ["general"]

    created = service.create(
        name="Reader",
        description="Read evidence.",
        system_prompt="Read carefully.",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=_registry(),
        expected_revision=0,
    )
    reader_id = created.definitions[-1].agent_id
    second = await runtime.run("second", session_id=first.session.session_id)

    persisted_first = store.load_run(first.session.session_id, first.run.run_id)
    assert persisted_first.agent_catalog == first.run.agent_catalog
    assert [entry.definition.agent_id for entry in second.run.agent_catalog.entries] == [
        "general",
        reader_id,
    ]


@pytest.mark.parametrize(
    "change,allowed",
    [
        ({"execution_contract_hash": "new", "owner_id": "dev.test@two#gen2"}, True),
        ({"canonical_tool_id": "dev.other/tools/read"}, False),
        ({"effect": "state_changing"}, False),
        ({"effect": "unknown"}, False),
        ({"source": "mcp"}, False),
    ],
)
def test_delegated_tool_ceiling_permits_updates_but_not_authority_expansion(change, allowed):
    from ouro.core.agents import EffectiveToolRef

    granted = EffectiveToolRef(
        public_name="read",
        canonical_tool_id="dev.test/tools/read",
        owner_id="dev.test@one#gen1",
        execution_contract_hash="old",
        effect="read_only",
    )
    assert granted.permits(granted.model_copy(update=change)) is allowed


@pytest.mark.parametrize("agent_id", ["general", "reader"])
def test_enabled_state_updates_live_catalog_and_preserves_frozen_contract(tmp_path, agent_id):
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    tools = _registry()
    created = service.copy_builtin("general", name="Reader", tools=tools, expected_revision=0)
    reader_id = created.definitions[-1].agent_id
    agent_id = "general" if agent_id == "general" else reader_id
    frozen = service.catalog_snapshot(tools)
    saved = service.set_enabled(agent_id, enabled=False, expected_revision=1, tools=tools)
    assert saved.revision == 2
    detail = service.get(agent_id, tools=tools)
    assert not detail.enabled
    assert detail.compatibility == "available"
    assert detail.contract_hash == frozen.resolve(agent_id).definition.contract_hash
    with pytest.raises(KeyError):
        service.catalog_snapshot(tools).resolve(agent_id)
    with pytest.raises(ConfigError, match="revision conflict"):
        service.set_enabled(agent_id, enabled=True, expected_revision=1, tools=tools)
    assert not service.get(agent_id, tools=tools).enabled
    reloaded = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    assert not reloaded.get(agent_id, tools=tools).enabled
    service.set_enabled(agent_id, enabled=True, expected_revision=2, tools=tools)
    assert service.catalog_snapshot(tools).resolve(agent_id) == frozen.resolve(agent_id)


def test_disabled_state_survives_edits_and_policy_changes_and_is_removed_on_delete(tmp_path):
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    tools = _registry()
    created = service.copy_builtin("general", name="Reader", tools=tools, expected_revision=0)
    reader_id = created.definitions[-1].agent_id
    service.set_enabled(reader_id, enabled=False, expected_revision=1, tools=tools)
    service.update(
        agent_id=reader_id,
        name="Renamed",
        description="Read",
        system_prompt="Read",
        tool_selection=ToolSelection(),
        model=None,
        tools=tools,
        expected_revision=2,
    )
    service.config.update_runtime(service.config.snapshot().runtime, expected_revision=3)
    assert not service.get(reader_id, tools=tools).enabled
    service.delete(reader_id, expected_revision=4, tools=tools)
    created = service.copy_builtin("general", name="Reader", tools=tools, expected_revision=5)
    reader_id = created.definitions[-1].agent_id
    assert service.get(reader_id, tools=tools).enabled
    with pytest.raises(ConfigError, match="not found"):
        service.set_enabled("missing", enabled=False, expected_revision=6, tools=tools)


@pytest.mark.asyncio
async def test_enablement_refreshes_between_decisions_in_the_same_run(tmp_path):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    registry = _registry()
    catalogs = []

    class SwitchingProvider(FauxProvider):
        async def stream(self, *, request_context=None, **kwargs):
            run = store.load_run(
                request_context.values["session.id"], request_context.values["run.id"]
            )
            catalogs.append(run.agent_catalog)
            turn = len(catalogs)
            if turn < 3:
                service.set_enabled(
                    "general", enabled=turn == 2, expected_revision=turn - 1, tools=registry
                )
                yield ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=ToolCallDelta(
                        index=0, id_delta=f"read-{turn}", name_delta="Read", arguments_delta="{}"
                    ),
                )
            else:
                yield ProviderStreamEvent(type="text_delta", text="done")
            yield ProviderStreamEvent(type="done")

    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            SwitchingProvider(), ModelMetadata(model_id="faux", provider_id="faux")
        ),
        tools=registry,
    )
    runtime.bind_extensions(agent_definitions=service)
    result = await runtime.run("Read twice")
    assert result.run.status == "completed"
    assert [catalog.revision for catalog in catalogs] == [0, 1, 2]
    assert [[entry.definition.agent_id for entry in catalog.entries] for catalog in catalogs] == [
        ["general"],
        [],
        ["general"],
    ]


@pytest.mark.parametrize(
    "name,prefix",
    [
        ("Code Reviewer", "code-reviewer_"),
        ("General", "general_"),
        ("123", "agent_"),
        ("审查员", "agent_"),
        ("A" * 80, "a" * 31 + "_"),
    ],
)
def test_framework_generates_unique_ids_and_rename_preserves_identity(tmp_path, name, prefix):
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    tools = _registry()
    first = service.create(
        name=name,
        description="Review",
        system_prompt="Review carefully",
        tool_selection=ToolSelection(),
        tools=tools,
        expected_revision=0,
    ).definitions[-1]
    second = service.copy_builtin(
        "general",
        name=name,
        tools=tools,
        expected_revision=1,
    ).definitions[-1]
    assert first.agent_id.startswith(prefix)
    assert second.agent_id.startswith(prefix)
    assert first.agent_id != second.agent_id
    assert len(first.agent_id) <= 64
    service.update(
        agent_id=first.agent_id,
        name="Renamed",
        description="Review",
        system_prompt="Review carefully",
        tool_selection=ToolSelection(),
        model=None,
        tools=tools,
        expected_revision=2,
    )
    reloaded = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    assert reloaded.get(first.agent_id, tools=tools).name == "Renamed"
    assert (
        reloaded.catalog_snapshot(tools).resolve(first.agent_id).definition.agent_id
        == first.agent_id
    )


@pytest.mark.parametrize("allowed_agents", [("*",), ("general",)])
def test_management_uses_active_whitelist_and_preserves_new_agent_enablement(
    tmp_path, allowed_agents
):
    config = AgentDefinitionsConfigManager(tmp_path)
    policy = AgentSpawnPolicy(allowed_agent_id_patterns=allowed_agents)
    config.update_runtime(
        config.snapshot().runtime.model_copy(update={"spawn_policy": policy}), expected_revision=0
    )
    service = AgentDefinitionService(config, spawn_policy=policy)
    tools = _registry()
    created = service.create(
        name="Reader",
        description="Read",
        system_prompt="Read",
        tool_selection=ToolSelection(),
        tools=tools,
        expected_revision=1,
    )
    agent = created.definitions[-1]
    assert agent.enabled
    assert agent.allowed_by_policy is (allowed_agents == ("*",))
    assert not created.policy_restart_required
    assert config.snapshot().runtime.spawn_policy.allowed_agent_id_patterns == allowed_agents
    # Saving a new whitelist cannot change the running Host's permissions.
    replacement = AgentSpawnPolicy(allowed_agent_id_patterns=("reader_*",))
    config.update_runtime(
        config.snapshot().runtime.model_copy(update={"spawn_policy": replacement}),
        expected_revision=2,
    )
    catalog = service.set_enabled(agent.agent_id, enabled=False, tools=tools, expected_revision=3)
    assert catalog.policy_restart_required
    assert catalog.definitions[-1].allowed_by_policy == agent.allowed_by_policy
    assert service.get(agent.agent_id, tools=tools).allowed_by_policy == agent.allowed_by_policy
    restarted = AgentDefinitionService(config, spawn_policy=replacement)
    assert restarted.get(agent.agent_id, tools=tools).allowed_by_policy
    assert not restarted.list(tools=tools).policy_restart_required
