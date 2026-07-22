from __future__ import annotations

import pytest

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.runtime import RuntimeService
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import ToolSelection
from ouro.core.errors import ConfigError
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

    snapshot = service.catalog_snapshot(_registry())
    assert [entry.definition.agent_id for entry in snapshot.entries] == ["general", "reader"]
    assert {item.public_name for item in snapshot.resolve("general").effective_tools} == {
        "Read",
        "Write",
    }
    assert [item.public_name for item in snapshot.resolve("reader").effective_tools] == [
        "Read"
    ]


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
        adapter=RuntimeAdapter(
            FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")
        ),
        tools=_registry(),
    )
    runtime.attach_agent_definitions(service)

    first = await runtime.run("first")
    assert first.run.agent_catalog is not None
    assert [entry.definition.agent_id for entry in first.run.agent_catalog.entries] == [
        "general"
    ]

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
