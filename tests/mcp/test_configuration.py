from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ouro.core.errors import ConfigError
from ouro.mcp import MCPConfigStore, MCPServerConfig, mcp_config_to_public_dict


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
    ],
)
def test_transport_configuration_is_strict(values, message) -> None:
    with pytest.raises(ValidationError, match=message):
        MCPServerConfig(server_id="test", name="Test", **values)
