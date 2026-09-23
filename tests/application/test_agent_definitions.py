from __future__ import annotations

import pytest

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.configuration import ConfigurationFacade
from ouro.application.runtime import RuntimeService
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentSpawnPolicy, ToolSelection
from ouro.core.errors import ConfigError
from ouro.core.model_selection import DEFAULT_MODEL
from ouro.core.provider import ModelMetadata
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


async def _handler(invocation):
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
        agent_id="reader",
        name="Reader",
        description="Read-only investigation.",
        system_prompt="Inspect the requested evidence.",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=_registry(),
        expected_revision=0,
    )

    assert [item.agent_id for item in created.definitions] == ["general", "reader"]
    assert created.definitions[0].editable is False
    assert created.definitions[1].editable is True
    assert "system_prompt" not in created.model_dump_json()
    assert service.get("reader", tools=_registry()).system_prompt == (
        "Inspect the requested evidence."
    )

    snapshot = service.catalog_snapshot(_registry())
    assert [entry.definition.agent_id for entry in snapshot.entries] == ["general", "reader"]
    assert {item.public_name for item in snapshot.resolve("general").effective_tools} == {
        "Read",
        "Write",
    }
    assert [item.public_name for item in snapshot.resolve("reader").effective_tools] == ["Read"]


def test_default_subagent_model_requires_explicit_system_route(tmp_path) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    options = dict(
        agent_id="reader",
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
    service.create(
        agent_id="reader",
        name="Reader",
        description="Read-only investigation.",
        system_prompt="Inspect evidence.",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=_registry(),
        expected_revision=0,
    )
    empty_registry = ToolRegistry()

    catalog = service.list(tools=empty_registry)
    reader = next(item for item in catalog.definitions if item.agent_id == "reader")
    assert reader.compatibility == "unavailable"
    assert reader.missing_tool_names == ("Read",)
    with pytest.raises(KeyError, match="unknown or unavailable"):
        service.catalog_snapshot(empty_registry).resolve("reader")


def test_definition_model_can_be_set_and_cleared_atomically(tmp_path) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    created = service.create(
        agent_id="reader",
        name="Reader",
        description="Read evidence.",
        system_prompt="Read carefully.",
        tool_selection=ToolSelection(mode="all"),
        model="faux/reviewer",
        tools=_registry(),
        expected_revision=0,
    )

    assert service.get("reader", tools=_registry()).model == "faux/reviewer"

    updated = service.update(
        agent_id="reader",
        name="Reader",
        description="Read evidence.",
        system_prompt="Read carefully.",
        tool_selection=ToolSelection(mode="all"),
        model=None,
        tools=_registry(),
        expected_revision=created.revision,
    )

    assert next(item for item in updated.definitions if item.agent_id == "reader").model is None


@pytest.mark.parametrize("selection_mode", ["all", "selected"])
def test_catalog_excludes_hard_denied_tools_and_freezes_approval_floor(
    tmp_path,
    selection_mode,
) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))
    if selection_mode == "selected":
        service.create(
            agent_id="reader",
            name="Reader",
            description="Read-only investigation.",
            system_prompt="Inspect evidence.",
            tool_selection=ToolSelection(mode="selected", names=("Read", "Write")),
            tools=_registry(),
            expected_revision=0,
        )
        agent_id = "reader"
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


def test_effective_tool_refs_freeze_declared_network_and_process_capabilities(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="community__fetch",
                description="Fetch through a community plugin.",
                source="third_party_plugin",
                effect="read_only",
                metadata={"plugin_permissions": ["network.outbound", "process.spawn"]},
            ),
            handler=_handler,
        )
    )
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))

    reference = service.catalog_snapshot(registry).resolve("general").effective_tools[0]

    assert reference.requires_network is True
    assert reference.requires_external_paths is True
    assert (
        AgentSpawnPolicy(
            allow_network=False,
            allow_external_paths=False,
        ).allows_tool(reference)
        is False
    )


def test_builtin_definition_is_immutable_but_can_be_copied(tmp_path) -> None:
    service = AgentDefinitionService(AgentDefinitionsConfigManager(tmp_path))

    with pytest.raises(ConfigError, match="immutable"):
        service.delete("general", expected_revision=0)
    copied = service.copy_builtin(
        "general",
        new_agent_id="custom",
        name="Custom",
        tools=_registry(),
        expected_revision=0,
    )

    custom = next(item for item in copied.definitions if item.agent_id == "custom")
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

    service.create(
        agent_id="reader",
        name="Reader",
        description="Read evidence.",
        system_prompt="Read carefully.",
        tool_selection=ToolSelection(mode="selected", names=("Read",)),
        tools=_registry(),
        expected_revision=0,
    )
    second = await runtime.run("second", session_id=first.session.session_id)

    persisted_first = store.load_run(first.session.session_id, first.run.run_id)
    assert persisted_first.agent_catalog == first.run.agent_catalog
    assert [entry.definition.agent_id for entry in second.run.agent_catalog.entries] == [
        "general",
        "reader",
    ]
