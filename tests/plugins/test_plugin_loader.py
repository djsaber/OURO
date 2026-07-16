from __future__ import annotations

import json

import pytest

from ouro.core.compaction import CompactionArtifact, CompactionStrategyRegistry
from ouro.core.hooks import HookContext, HookRegistration, HookRegistry, HookResult
from ouro.plugins import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginContextProvider,
    PluginContributions,
    PluginLoader,
    PluginPermissions,
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
                "runtime_hooks": [{"hook_name": "after_context_build"}],
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
    assert plugins[1].source == "third_party"


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
                "runtime_hooks": [{"hook_name": "after_context_build"}],
            }
        ),
        encoding="utf-8",
    )
    loader = PluginLoader(
        tmp_path,
        enabled_plugin_ids={"official.echo"},
        trusted_source="official",
    )
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
    loader = PluginLoader(
        tmp_path,
        enabled_plugin_ids={"official.assistant"},
        trusted_source="official",
    )
    plugin = loader.discover()[0]
    tool_registry = ToolRegistry()
    hook_registry = HookRegistry()
    command_registry = PluginCommandRegistry()

    result = loader.register_contributions(
        plugin,
        PluginContributions(
            commands=[
                PluginCommand(
                    name="hello",
                    description="Say hello.",
                    handler=lambda request: f"hello {request.argument}",
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

    command_output = await command_registry.execute("official.assistant:hello", "OURO")
    dispatch = await hook_registry.dispatch(HookContext(hook_name="after_context_build"))

    assert result.errors == []
    assert result.registered_commands == ["official.assistant:hello"]
    assert result.registered_context_providers == ["profile"]
    assert command_output.output == "hello OURO"
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


def test_plugin_contributes_explicit_compaction_strategy(tmp_path) -> None:
    class PluginSummaryStrategy:
        strategy_id = "plugin.echo.summary"
        version = "1"

        async def compact(self, request, *, signal=None):
            del request, signal
            return CompactionArtifact(summary="plugin summary")

    plugin_dir = tmp_path / "summary"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "third.summary",
                "name": "Summary",
                "version": "1.0.0",
                "source_type": "third_party",
                "capabilities": ["compaction_strategies"],
            }
        ),
        encoding="utf-8",
    )
    loader = PluginLoader(tmp_path, enabled_plugin_ids={"third.summary"})
    plugin = loader.discover()[0]
    registry = CompactionStrategyRegistry()

    result = loader.register_contributions(
        plugin,
        PluginContributions(compaction_strategies=[PluginSummaryStrategy()]),
        tool_registry=ToolRegistry(),
        hook_registry=HookRegistry(),
        compaction_registry=registry,
    )

    assert result.errors == []
    assert result.registered_compaction_strategies == ["plugin.echo.summary"]
    assert registry.get("plugin.echo.summary").strategy_id == "plugin.echo.summary"


def test_plugin_cannot_shadow_reserved_builtin_compaction_strategy(tmp_path) -> None:
    class ShadowStrategy:
        strategy_id = "builtin.llm_summary"
        version = "1"

        async def compact(self, request, *, signal=None):
            del request, signal
            return CompactionArtifact(summary="shadow")

    plugin_dir = tmp_path / "shadow"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "third.shadow",
                "name": "Shadow",
                "version": "1.0.0",
                "source_type": "third_party",
                "capabilities": ["compaction_strategies"],
            }
        ),
        encoding="utf-8",
    )
    loader = PluginLoader(tmp_path, enabled_plugin_ids={"third.shadow"})

    result = loader.register_contributions(
        loader.discover()[0],
        PluginContributions(compaction_strategies=[ShadowStrategy()]),
        tool_registry=ToolRegistry(),
        hook_registry=HookRegistry(),
        compaction_registry=CompactionStrategyRegistry(),
    )

    assert result.registered_compaction_strategies == []
    assert result.errors == [
        "plugin compaction strategy cannot use reserved id: builtin.llm_summary"
    ]


def test_manifest_cannot_self_promote_tool_to_official_or_auto_execute(tmp_path) -> None:
    plugin_dir = tmp_path / "untrusted"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "example.untrusted",
                "name": "Untrusted",
                "version": "1.0.0",
                "source_type": "official",
                "capabilities": ["tools"],
            }
        ),
        encoding="utf-8",
    )
    loader = PluginLoader(tmp_path, enabled_plugin_ids={"example.untrusted"})
    registry = ToolRegistry()

    result = loader.register_contributions(
        loader.discover()[0],
        PluginContributions(
            tools=[
                Tool(
                    definition=ToolDefinition(name="Untrusted", description="Untrusted."),
                    handler=lambda _invocation: ToolResult(
                        tool_call_id="call",
                        tool_name="Untrusted",
                    ),
                )
            ]
        ),
        tool_registry=registry,
        hook_registry=HookRegistry(),
    )

    assert result.errors == []
    definition = registry.definitions()[0]
    assert definition.source == "third_party_plugin"
    assert definition.permission_mode == "approval"


def test_plugin_permissions_require_explicit_origin_grant(tmp_path) -> None:
    plugin_dir = tmp_path / "network"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "example.network",
                "name": "Network",
                "version": "1.0.0",
                "permissions": {"network": True},
            }
        ),
        encoding="utf-8",
    )

    unauthorized = PluginLoader(
        tmp_path,
        enabled_plugin_ids={"example.network"},
    ).discover()[0]
    authorized = PluginLoader(
        tmp_path,
        enabled_plugin_ids={"example.network"},
        permission_grants={
            "example.network": PluginPermissions(network=True),
        },
    ).discover()[0]

    assert unauthorized.status == "needs_authorization"
    assert unauthorized.enabled is False
    assert unauthorized.error == "plugin permissions require authorization: network"
    assert authorized.status == "enabled"
    assert authorized.enabled is True


def test_plugin_contribution_requires_declared_capability(tmp_path) -> None:
    plugin_dir = tmp_path / "minimal"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "example.minimal",
                "name": "Minimal",
                "version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    loader = PluginLoader(tmp_path, enabled_plugin_ids={"example.minimal"})

    result = loader.register_contributions(
        loader.discover()[0],
        PluginContributions(
            tools=[
                Tool(
                    definition=ToolDefinition(name="Hidden", description="Hidden."),
                    handler=lambda _invocation: ToolResult(
                        tool_call_id="call",
                        tool_name="Hidden",
                    ),
                )
            ]
        ),
        tool_registry=registry,
        hook_registry=HookRegistry(),
    )

    assert result.errors == ["plugin contribution requires undeclared capability: tools"]
    assert registry.definitions() == []


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
