from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.cli.errors import error_exit_code
from ouro.core.errors import OuroError
from ouro.mcp import MCPServerConfigUpdate


def list_mcp_configs(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        snapshot = ConfigurationFacade(home).mcp_catalog()
    except OuroError as exc:
        _print_mcp_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    servers = [config.model_dump(mode="json") for config in snapshot.servers]
    if json_mode:
        _print_json(
            status="ok",
            config_path=snapshot.config_path,
            servers=servers,
        )
        return 0

    lines = ["MCP servers:"]
    for server in servers:
        enabled = "enabled" if server["enabled"] else "disabled"
        auto_start = "auto" if server["auto_start"] else "manual"
        target = server["command"] if server["transport"] == "stdio" else server["url"]
        lines.append(
            f"- {server['name']} {server['transport']} target={target} {enabled} {auto_start}"
        )
    if not servers:
        lines.append("(none)")
    print("\n".join(lines))
    return 0


def show_mcp_config(
    server_name: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        config = facade.mcp_server_by_name(server_name)
    except OuroError as exc:
        _print_mcp_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = config.model_dump(mode="json")
    if json_mode:
        _print_json(
            status="ok",
            config_path=str(facade.mcp.config_path),
            server=public,
        )
        return 0

    target = public["command"] if public["transport"] == "stdio" else public["url"]
    print(
        "\n".join(
            [
                f"MCP server: {public['name']}",
                f"Transport: {public['transport']}",
                f"Target: {target}",
                f"Enabled: {'yes' if public['enabled'] else 'no'}",
                f"Auto start: {'yes' if public['auto_start'] else 'no'}",
                f"Auto reconnect: {'yes' if public['auto_reconnect'] else 'no'}",
                f"Permission: {public['permission_mode']}",
                f"Tool effects: {public['tool_effect_overrides']}",
                f"Config: {facade.mcp.config_path}",
            ]
        )
    )
    return 0


def set_mcp_config(
    server_name: str,
    update: MCPServerConfigUpdate,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        config = facade.set_mcp_server_by_name(server_name, update)
    except OuroError as exc:
        _print_mcp_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = config.model_dump(mode="json")
    if json_mode:
        _print_json(
            status="ok",
            config_path=str(facade.mcp.config_path),
            server=public,
        )
        return 0
    print(f"MCP server saved: {config.name}")
    return 0


def remove_mcp_config(
    server_name: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        config = facade.mcp_server_by_name(server_name)
        catalog = facade.remove_mcp_server(config.server_id)
    except OuroError as exc:
        _print_mcp_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        _print_json(
            status="ok",
            config_path=catalog.config_path,
            removed=server_name,
        )
        return 0
    print(f"MCP server removed: {server_name}")
    return 0


async def check_mcp_connection(
    server_name: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        config = facade.mcp_server_by_name(server_name)
        check = await facade.check_mcp_server(config.server_id)
    except OuroError as exc:
        _print_mcp_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    result: dict[str, Any] = {
        "status": "ok" if check.ok else "failed",
        "config_path": str(facade.mcp.config_path),
        "server": check.server.model_dump(mode="json"),
        "connection_status": check.status,
        "server_info": check.server_info,
        "tools": list(check.tool_names),
        "error": check.error,
        "diagnostics": [
            item.model_dump(mode="json", exclude_none=True) for item in check.diagnostics
        ],
    }

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "ok":
        info = result["server_info"]
        implementation_name = info["name"] if isinstance(info, dict) else config.name
        print(f"MCP check OK: {config.name} ({implementation_name}), tools={len(check.tool_names)}")
    else:
        print(f"MCP check failed: {server_name} - {result['error']}", file=sys.stderr)
    return 0 if result["status"] == "ok" else 3


def _print_mcp_error(exc: OuroError, *, json_mode: bool) -> None:
    if json_mode:
        _print_json(
            status="failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        print(f"Error: {exc}", file=sys.stderr)


def _print_json(**payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False))
