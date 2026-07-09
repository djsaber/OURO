from __future__ import annotations

import json

import pytest

from ouro.core.hooks import HookContext, HookRegistration, HookRegistry, HookResult
from ouro.plugins import (
    PluginCommand,
    PluginCommandRegistry,
    PluginContextProvider,
    PluginContributions,
    PluginLoader,
)
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


def test_plugin_loader_discovers_manifest_and_isolates_bad_metadata(tmp_path) -> None:
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    (good / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "official.echo",
                "name": "Echo",
                "version": "0.1.0",
                "source_type": "official",
                "capabilities": ["tools", "runtime_hooks"],
            }
        ),
        encoding="utf-8",
    )
    (bad / "plugin.json").write_text("{not-json", encoding="utf-8")

    plugins = PluginLoader(tmp_path, enabled_plugin_ids={"official.echo"}).discover()

    assert len(plugins) == 2
    assert plugins[0].status == "failed"
    assert plugins[0].error
    assert plugins[1].manifest is not None
    assert plugins[1].manifest.plugin_id == "official.echo"
    assert plugins[1].enabled is True


@pytest.mark.asyncio
async def test_plugin_contributions_register_tools_and_hooks(tmp_path) -> None:
    plugin_dir = tmp_path / "echo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "official.echo",
                "name": "Echo",
                "version": "0.1.0",
                "source_type": "official",
                "capabilities": ["tools", "runtime_hooks"],
            }
        ),
        encoding="utf-8",
    )
    loader = PluginLoader(tmp_path, enabled_plugin_ids={"official.echo"})
    plugin = loader.discover()[0]
    tool_registry = ToolRegistry()
    hook_registry = HookRegistry()

    def echo_tool(_invocation):
        return ToolResult(tool_call_id="call_1", tool_name="Echo", content="ok")

    def hook(_context: HookContext) -> HookResult:
        return HookResult(added_context=["plugin context"])

    result = loader.register_contributions(
        plugin,
        PluginContributions(
            tools=[
                Tool(
                    definition=ToolDefinition(name="Echo", description="Echo input."),
                    handler=echo_tool,
                )
            ],
            hooks=[
                HookRegistration(
                    plugin_id="placeholder",
                    hook_name="after_context_build",
                    handler=hook,
                )
            ],
        ),
        tool_registry=tool_registry,
        hook_registry=hook_registry,
    )

    assert result.errors == []
    assert result.registered_tools == ["Echo"]
    assert result.registered_hooks == ["after_context_build"]
    assert tool_registry.definitions()[0].source == "official_plugin"
    registered_hook = hook_registry.registered("after_context_build")[0]
    assert registered_hook.plugin_id == "official.echo"
    assert registered_hook.source == "official"

    dispatch = await hook_registry.dispatch(HookContext(hook_name="after_context_build"))
    assert dispatch.results[0].added_context == ["plugin context"]


@pytest.mark.asyncio
async def test_plugin_contributions_register_commands_and_context_providers(tmp_path) -> None:
    plugin_dir = tmp_path / "assistant"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "official.assistant",
                "name": "Assistant",
                "version": "0.1.0",
                "source_type": "official",
                "capabilities": ["commands", "context_providers"],
            }
        ),
        encoding="utf-8",
    )
    loader = PluginLoader(tmp_path, enabled_plugin_ids={"official.assistant"})
    plugin = loader.discover()[0]
    tool_registry = ToolRegistry()
    hook_registry = HookRegistry()
    command_registry = PluginCommandRegistry()

    result = loader.register_contributions(
        plugin,
        PluginContributions(
            commands=[
                PluginCommand(
                    name="/hello",
                    description="Say hello.",
                    handler=lambda argument, _context: f"hello {argument}",
                )
            ],
            context_providers=[
                PluginContextProvider(
                    provider_id="profile",
                    handler=lambda _context: "remember user preference",
                )
            ],
        ),
        tool_registry=tool_registry,
        hook_registry=hook_registry,
        command_registry=command_registry,
    )

    command_output = await command_registry.execute("/hello", "OURO")
    dispatch = await hook_registry.dispatch(HookContext(hook_name="after_context_build"))

    assert result.errors == []
    assert result.registered_commands == ["/hello"]
    assert result.registered_context_providers == ["profile"]
    assert command_output == "hello OURO"
    assert dispatch.results[0].added_context == ["remember user preference"]


def test_disabled_plugin_does_not_register_contributions(tmp_path) -> None:
    plugin_dir = tmp_path / "echo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "third.echo",
                "name": "Echo",
                "version": "0.1.0",
                "source_type": "third_party",
            }
        ),
        encoding="utf-8",
    )
    loader = PluginLoader(tmp_path)
    plugin = loader.discover()[0]

    result = loader.register_contributions(
        plugin,
        PluginContributions(),
        tool_registry=ToolRegistry(),
        hook_registry=HookRegistry(),
    )

    assert result.errors == ["plugin is disabled"]
