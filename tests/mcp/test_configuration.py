from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ouro.core.errors import ConfigError
from ouro.mcp import (
    MCPConfigStore,
    MCPManager,
    MCPRuntimeResource,
    MCPServerConfig,
    MCPServerConfigUpdate,
    mcp_config_to_public_dict,
)
from ouro.tools import ToolRegistry


@pytest.mark.asyncio
async def test_runtime_resource_exposes_host_lifecycle_without_manager_access() -> None:
    manager = MCPManager()
    manager.configure(
        MCPServerConfig(
            server_id="disabled",
            name="Disabled",
            command="python",
            enabled=False,
        )
    )
    resource = MCPRuntimeResource(manager)

    assert resource.records[0].status == "disabled"
    assert (await resource.start_server("disabled")).status == "disabled"
    assert (await resource.stop_server("disabled")).status == "disabled"


def test_mcp_config_store_round_trips_without_overwriting_other_sections(tmp_path) -> None:
    config_path = tmp_path / "config" / "prompts.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"prompts": {"system": "keep"}}), encoding="utf-8")
    store = MCPConfigStore(tmp_path)

    config = store.update(
        None,
        MCPServerConfigUpdate(
            name="Remote",
            transport="http",
            url="https://mcp.example.test/mcp",
            credential_refs={"Authorization": "env:OURO_MCP_TOKEN"},
        ),
    )

    snapshot = store.snapshot()
    assert snapshot.servers[0].server_id == config.server_id
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["prompts"] == {"system": "keep"}
    raw = json.loads(store.config_path.read_text(encoding="utf-8"))
    assert raw["mcp"]["servers"][config.server_id]["url"] == "https://mcp.example.test/mcp"

    removed = store.remove(config.server_id)
    assert removed.server_id == config.server_id
    assert store.snapshot().servers == ()


def test_mcp_config_store_rejects_noncanonical_shape(tmp_path) -> None:
    config_path = tmp_path / "config" / "mcp.json"
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

    assert "server_id" not in public
    assert public["credential_refs"] == {
        "Authorization": "inline:***",
        "X-Api-Key": "env:OURO_MCP_KEY",
    }


def test_store_allocates_identity_and_preserves_it_across_rename_and_reload(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    created = store.update(None, MCPServerConfigUpdate(name="  本地工具  ", command="python"))

    assert created.name == "本地工具"
    assert created.server_id != created.name
    renamed = store.update(created.server_id, MCPServerConfigUpdate(name="文件工具"))
    reloaded = MCPConfigStore(tmp_path).get(created.server_id)
    assert renamed.server_id == created.server_id
    assert reloaded == renamed
    assert store.find_by_name("本地工具") is None
    assert store.find_by_name(" 文件工具 ") == renamed
    assert store.find_by_name(created.server_id) is None


def test_duplicate_names_reject_create_and_rename_without_writing(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    store.update(None, MCPServerConfigUpdate(name="First", command="python"))
    second = store.update(None, MCPServerConfigUpdate(name="Second", command="python"))
    before = store.config_path.read_bytes()

    for server_id in (None, second.server_id):
        with pytest.raises(ConfigError, match="display name already exists: First"):
            store.update(server_id, MCPServerConfigUpdate(name=" First ", command="python"))
        assert store.config_path.read_bytes() == before


def test_named_updates_keep_identity_and_reject_duplicate_persisted_names(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    first = store.set_by_name("Local", MCPServerConfigUpdate(command="python"))
    updated = store.set_by_name("Local", MCPServerConfigUpdate(args=["server.py"]))
    assert updated.server_id == first.server_id
    assert len(store.snapshot().servers) == 1

    raw = json.loads(store.config_path.read_text(encoding="utf-8"))
    raw["mcp"]["servers"]["duplicate"] = raw["mcp"]["servers"][first.server_id]
    store.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="display name already exists"):
        store.snapshot()


def test_creation_requires_name_and_update_requires_existing_identity(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    with pytest.raises(ConfigError, match="name"):
        store.update(None, MCPServerConfigUpdate(command="python"))
    with pytest.raises(ConfigError, match="must not be blank"):
        store.update(None, MCPServerConfigUpdate(name="  ", command="python"))
    with pytest.raises(ConfigError, match="not found") as error:
        store.update("unknown-internal-id", MCPServerConfigUpdate(name="Local", command="python"))
    assert "unknown-internal-id" not in str(error.value)
    assert not store.config_path.exists()


def test_user_update_cannot_supply_server_identity() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MCPServerConfigUpdate.model_validate({"server_id": "chosen", "name": "Local"})


def test_persisted_config_rejects_user_identity_field(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    created = store.update(None, MCPServerConfigUpdate(name="Local", command="python"))
    raw = json.loads(store.config_path.read_text(encoding="utf-8"))
    raw["mcp"]["servers"][created.server_id]["server_id"] = created.server_id
    store.config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="internal and cannot be configured"):
        store.snapshot()


def test_config_update_switches_transport_without_stale_fields(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    store.set_by_name(
        "Remote",
        MCPServerConfigUpdate(
            name="Remote",
            transport="http",
            url="https://mcp.example.test/mcp",
            headers={"X-Static": "value"},
            credential_refs={"Authorization": "env:OURO_MCP_TOKEN"},
        ),
    )

    config = store.set_by_name(
        "Remote",
        MCPServerConfigUpdate(transport="stdio", command="python"),
    )

    assert config.transport == "stdio"
    assert config.command == "python"
    assert config.url is None
    assert config.headers == {}
    assert config.credential_refs == {}


def test_config_update_preserves_compatible_fields_between_remote_transports(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    original = store.set_by_name(
        "Remote",
        MCPServerConfigUpdate(
            transport="http",
            url="https://mcp.example.test/mcp",
            headers={"X-Static": "value"},
            credential_refs={"Authorization": "env:OURO_MCP_TOKEN"},
        ),
    )

    updated = store.set_by_name("Remote", MCPServerConfigUpdate(transport="sse"))

    assert updated.url == original.url
    assert updated.headers == original.headers
    assert updated.credential_refs == original.credential_refs


def test_config_update_replaces_collections_and_is_atomic_on_error(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    original = store.set_by_name(
        "Local",
        MCPServerConfigUpdate(
            command="python",
            args=["server.py"],
            env={"MODE": "first"},
        ),
    )
    config_path = store.config_path
    before = config_path.read_bytes()

    updated = store.set_by_name(
        "Local",
        MCPServerConfigUpdate(args=[], env={"MODE": "second"}),
    )

    assert updated.args == []
    assert updated.env == {"MODE": "second"}
    after_valid_update = config_path.read_bytes()
    with pytest.raises(ConfigError, match="url is required"):
        store.set_by_name("Local", MCPServerConfigUpdate(transport="http"))
    assert store.get(original.server_id) == updated
    assert store.get(original.server_id) != original
    assert config_path.read_bytes() != before
    assert config_path.read_bytes() == after_valid_update


def test_config_validation_error_does_not_expose_inline_credentials(tmp_path) -> None:
    secret = "do-not-print-this"

    with pytest.raises(ConfigError) as captured:
        MCPConfigStore(tmp_path).set_by_name(
            "Remote",
            MCPServerConfigUpdate(
                transport="http",
                url="https://mcp.example.test/mcp",
                credential_refs={"invalid target": f"inline:{secret}"},
            ),
        )

    assert secret not in str(captured.value)


@pytest.mark.asyncio
async def test_runtime_resource_leaves_explicit_non_auto_server_disconnected(tmp_path) -> None:
    config = MCPConfigStore(tmp_path).update(
        None,
        MCPServerConfigUpdate(
            name="Manual",
            command="not-started",
            auto_start=False,
        ),
    )
    registry = ToolRegistry()

    async with MCPRuntimeResource.from_config(tmp_path, registry) as resource:
        assert resource.startup_records == ()
        assert resource.manager.get(config.server_id).status == "configured"
        assert registry.names() == []


def test_runtime_resource_exposes_only_enabled_auto_start_configs(tmp_path) -> None:
    store = MCPConfigStore(tmp_path)
    auto = store.update(
        None,
        MCPServerConfigUpdate(
            name="Auto",
            command="auto-server",
            auto_start=True,
        ),
    )
    store.update(
        None,
        MCPServerConfigUpdate(
            name="Manual",
            command="manual-server",
            auto_start=False,
        ),
    )
    store.update(
        None,
        MCPServerConfigUpdate(
            name="Disabled",
            command="disabled-server",
            enabled=False,
            auto_start=True,
        ),
    )

    resource = MCPRuntimeResource.from_config(tmp_path, ToolRegistry())

    assert [config.server_id for config in resource.startup_configs] == [auto.server_id]


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
