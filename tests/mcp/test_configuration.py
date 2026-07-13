from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ouro.core.errors import ConfigError
from ouro.mcp import (
    MCPConfigStore,
    MCPRuntimeResource,
    MCPServerConfig,
    MCPServerConfigUpdate,
    mcp_config_to_public_dict,
)
from ouro.tools import ToolRegistry


def test_mcp_config_store_round_trips_without_overwriting_other_sections(tmp_path) -> None:
    config_path = tmp_path / "config" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"prompts": {"system": "keep"}}), encoding="utf-8")
    store = MCPConfigStore(tmp_path)

    store.upsert(
        MCPServerConfig(
            server_id="remote",
            name="Remote",
            transport="http",
            url="https://mcp.example.test/mcp",
            credential_refs={"Authorization": "env:OURO_MCP_TOKEN"},
        )
    )

    snapshot = store.snapshot()
    assert snapshot.servers[0].server_id == "remote"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["prompts"] == {"system": "keep"}
    assert raw["mcp"]["servers"]["remote"]["url"] == "https://mcp.example.test/mcp"

    removed = store.remove("remote")
    assert removed.server_id == "remote"
    assert store.snapshot().servers == ()


def test_mcp_config_store_rejects_noncanonical_shape(tmp_path) -> None:
    config_path = tmp_path / "config" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"mcp": {"servers": [], "legacy": True}}), encoding="utf-8")

    with pytest.raises(ConfigError, match="unsupported mcp config fields"):
        MCPConfigStore(tmp_path).snapshot()


def test_public_config_redacts_inline_credentials() -> None:
    config = MCPServerConfig(
        server_id="remote",
        name="Remote",
        transport="http",
        url="https://mcp.example.test/mcp",
        credential_refs={
            "Authorization": "inline:secret",
            "X-Api-Key": "env:OURO_MCP_KEY",
        },
    )

    public = mcp_config_to_public_dict(config)

    assert public["credential_refs"] == {
        "Authorization": "inline:***",
        "X-Api-Key": "env:OURO_MCP_KEY",
    }


def test_config_update_switches_transport_without_stale_fields(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    store.update(
        "remote",
        MCPServerConfigUpdate(
            name="Remote",
            transport="http",
            url="https://mcp.example.test/mcp",
            headers={"X-Static": "value"},
            credential_refs={"Authorization": "env:OURO_MCP_TOKEN"},
        ),
    )

    config = store.update(
        "remote",
        MCPServerConfigUpdate(transport="stdio", command="python"),
    )

    assert config.transport == "stdio"
    assert config.command == "python"
    assert config.url is None
    assert config.headers == {}
    assert config.credential_refs == {}


def test_config_update_preserves_compatible_fields_between_remote_transports(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    original = store.update(
        "remote",
        MCPServerConfigUpdate(
            transport="http",
            url="https://mcp.example.test/mcp",
            headers={"X-Static": "value"},
            credential_refs={"Authorization": "env:OURO_MCP_TOKEN"},
        ),
    )

    updated = store.update("remote", MCPServerConfigUpdate(transport="sse"))

    assert updated.url == original.url
    assert updated.headers == original.headers
    assert updated.credential_refs == original.credential_refs


def test_config_update_replaces_collections_and_is_atomic_on_error(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    original = store.update(
        "local",
        MCPServerConfigUpdate(
            command="python",
            args=["server.py"],
            env={"MODE": "first"},
        ),
    )
    config_path = store.config_path
    before = config_path.read_bytes()

    updated = store.update(
        "local",
        MCPServerConfigUpdate(args=[], env={"MODE": "second"}),
    )

    assert updated.args == []
    assert updated.env == {"MODE": "second"}
    after_valid_update = config_path.read_bytes()
    with pytest.raises(ConfigError, match="url is required"):
        store.update("local", MCPServerConfigUpdate(transport="http"))
    assert store.get("local") == updated
    assert store.get("local") != original
    assert config_path.read_bytes() != before
    assert config_path.read_bytes() == after_valid_update


def test_config_validation_error_does_not_expose_inline_credentials(tmp_path) -> None:
    secret = "do-not-print-this"

    with pytest.raises(ConfigError) as captured:
        MCPConfigStore(tmp_path).update(
            "remote",
            MCPServerConfigUpdate(
                transport="http",
                url="https://mcp.example.test/mcp",
                credential_refs={"invalid target": f"inline:{secret}"},
            ),
        )

    assert secret not in str(captured.value)


@pytest.mark.asyncio
async def test_runtime_resource_leaves_explicit_non_auto_server_disconnected(tmp_path) -> None:
    MCPConfigStore(tmp_path).upsert(
        MCPServerConfig(
            server_id="manual",
            name="Manual",
            command="not-started",
            auto_start=False,
        )
    )
    registry = ToolRegistry()

    async with MCPRuntimeResource.from_config(tmp_path, registry) as resource:
        assert resource.startup_records == ()
        assert resource.manager.get("manual").status == "configured"
        assert registry.names() == []


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"transport": "stdio"}, "command is required"),
        ({"transport": "http", "command": "server"}, "url is required"),
        (
            {"transport": "http", "url": "file:///tmp/server"},
            "must use http or https",
        ),
        (
            {"transport": "http", "url": "https://example.test/mcp", "args": ["x"]},
            "not valid for http transport",
        ),
        (
            {"transport": "stdio", "command": "server", "headers": {"X-Test": "x"}},
            "headers are not valid for stdio transport",
        ),
    ],
)
def test_transport_configuration_is_strict(values, message) -> None:
    with pytest.raises(ValidationError, match=message):
        MCPServerConfig(server_id="test", name="Test", **values)
