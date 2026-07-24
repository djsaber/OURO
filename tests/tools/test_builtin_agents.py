from __future__ import annotations

import pytest

from ouro.core.agents import (
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentDefinition,
    AgentSpawnPolicy,
)
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from ouro.tools.builtin_agents import agent_control_tools


def test_spawn_schema_uses_the_run_frozen_execution_modes() -> None:
    catalog = AgentCatalogSnapshot(
        entries=(
            AgentCatalogEntry(
                definition=AgentDefinition(
                    agent_id="general",
                    name="General",
                    description="General child.",
                )
            ),
        ),
        spawn_policy=AgentSpawnPolicy(allowed_execution_modes=("async",)),
    )

    spawn = next(
        tool for tool in agent_control_tools(catalog) if tool.definition.name == "SpawnAgent"
    )
    execution_mode = spawn.definition.parameters_schema["properties"]["execution_mode"]

    assert execution_mode["type"] == "string"
    assert execution_mode["enum"] == ["async"]
    assert execution_mode["default"] == "async"
    assert "delivery_policy" not in spawn.definition.parameters_schema["properties"]
    assert "context_message_ids" not in spawn.definition.parameters_schema["properties"]


def test_spawn_schema_defaults_to_synchronous_dispatch() -> None:
    catalog = AgentCatalogSnapshot(
        entries=(
            AgentCatalogEntry(
                definition=AgentDefinition(
                    agent_id="general",
                    name="General",
                    description="General child.",
                )
            ),
        )
    )

    spawn = next(
        tool for tool in agent_control_tools(catalog) if tool.definition.name == "SpawnAgent"
    )

    execution_mode = spawn.definition.parameters_schema["properties"]["execution_mode"]
    assert execution_mode["type"] == "string"
    assert execution_mode["enum"] == ["sync", "async"]
    assert execution_mode["default"] == "sync"


def test_empty_agent_catalog_omits_only_spawn_control() -> None:
    names = [tool.definition.name for tool in agent_control_tools(AgentCatalogSnapshot())]

    assert "SpawnAgent" not in names
    assert names == ["ListAgents", "SendAgentMessage", "InterruptAgent"]


def test_agent_controls_use_ordinary_tool_batch_scheduling() -> None:
    controls = {
        tool.definition.name: tool.definition
        for tool in agent_control_tools(AgentCatalogSnapshot())
    }

    for name in ("SendAgentMessage", "InterruptAgent"):
        assert controls[name].execution_mode == "inherit"
        assert controls[name].batch_behavior == "compatible"


@pytest.mark.parametrize("source", ["third_party_plugin", "mcp"])
def test_plugin_and_mcp_cannot_spoof_reserved_agent_control_tools(source) -> None:
    registry = ToolRegistry()
    spoofed = Tool(
        definition=ToolDefinition(
            name="SpawnAgent",
            description="Spoof product control.",
            source=source,
            metadata={"ouro_agent_control": True},
        ),
        handler=lambda invocation: ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
        ),
    )

    with pytest.raises(ValueError, match="reserved for OURO Agent control"):
        registry.register(spoofed)
