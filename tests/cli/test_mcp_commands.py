from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ouro.cli.main import main
from ouro.cli.parser import build_parser
from ouro.mcp import MCPConfigStore


def test_mcp_cli_set_list_show_and_remove(tmp_path, capsys) -> None:
    set_code = main(
        [
            "mcp",
            "set",
            "local",
            "--home",
            str(tmp_path),
            "--name",
            "Local tools",
            "--command",
            sys.executable,
            "--arg",
            "server.py",
            "--env",
            "MODE=test",
            "--auto-start",
            "--permission-mode",
            "auto",
            "--tool-effect",
            "read=read_only",
            "--json",
        ]
    )
    assert set_code == 0
    saved = json.loads(capsys.readouterr().out)["server"]
    assert "server_id" not in saved
    assert saved["name"] == "Local tools"
    assert saved["args"] == ["***"]
    assert saved["env"] == {"MODE": "***"}
    assert saved["auto_start"] is True

    assert main(["mcp", "list", "--home", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [server["name"] for server in listed["servers"]] == ["Local tools"]

    assert main(["mcp", "show", "Local tools", "--home", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)["server"]
    assert shown["name"] == "Local tools"
    assert shown["permission_mode"] == "auto"
    assert shown["tool_effect_overrides"] == {"read": "read_only"}

    assert main(["mcp", "remove", "Local tools", "--home", str(tmp_path), "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] == "Local tools"
    assert MCPConfigStore(tmp_path).snapshot().servers == ()


def test_mcp_cli_rejects_invalid_tool_effect(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["mcp", "set", "local", "--tool-effect", "read=harmless"])

    assert exc_info.value.code == 2
    assert "tool effect must be read_only, state_changing, or unknown" in capsys.readouterr().err


def test_mcp_cli_names_and_rename_never_expose_internal_identity(tmp_path, capsys) -> None:
    common = ["--home", str(tmp_path)]
    assert main(["mcp", "set", "本地工具", "--command", "python", *common]) == 0
    created = MCPConfigStore(tmp_path).find_by_name("本地工具")
    assert created is not None
    assert created.server_id not in capsys.readouterr().out

    for command in (["list"], ["show", "本地工具"]):
        for output in ([], ["--json"]):
            assert main(["mcp", *command, *common, *output]) == 0
            result = capsys.readouterr().out
            assert "本地工具" in result
            assert created.server_id not in result
            assert "server_id" not in result

    assert main(["mcp", "set", "本地工具", "--name", "文件工具", *common, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["server"]["name"] == "文件工具"
    renamed = MCPConfigStore(tmp_path).find_by_name("文件工具")
    assert renamed is not None
    assert renamed.server_id == created.server_id
    assert main(["mcp", "show", "本地工具", *common]) == 2
    capsys.readouterr()
    assert main(["mcp", "show", created.server_id, *common]) == 2
    capsys.readouterr()
    assert main(["mcp", "remove", "文件工具", *common]) == 0
    result = capsys.readouterr().out
    assert "文件工具" in result
    assert created.server_id not in result


def test_mcp_cli_replaces_and_clears_collections(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "mcp",
                "set",
                "local",
                "--home",
                str(tmp_path),
                "--command",
                "python",
                "--arg",
                "one",
                "--arg",
                "two",
                "--env",
                "FIRST=1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "mcp",
                "set",
                "local",
                "--home",
                str(tmp_path),
                "--clear-args",
                "--env",
                "SECOND=2",
                "--json",
            ]
        )
        == 0
    )
    server = json.loads(capsys.readouterr().out)["server"]
    assert server["args"] == []
    assert server["env"] == {"SECOND": "***"}

    assert (
        main(
            [
                "mcp",
                "set",
                "local",
                "--home",
                str(tmp_path),
                "--clear-env",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["server"]["env"] == {}


def test_mcp_cli_updates_lifecycle_and_stdio_options(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "mcp",
                "set",
                "local",
                "--home",
                str(tmp_path),
                "--command",
                "python",
                "--inherit-env",
                "PATH",
                "--cwd",
                str(tmp_path),
                "--no-auto-reconnect",
                "--max-reconnect-attempts",
                "5",
                "--reconnect-backoff",
                "1.5",
                "--timeout",
                "12",
                "--health-check-interval",
                "7",
                "--json",
            ]
        )
        == 0
    )
    server = json.loads(capsys.readouterr().out)["server"]
    assert server["inherit_env"] == ["PATH"]
    assert server["cwd"] == str(tmp_path)
    assert server["auto_reconnect"] is False
    assert server["max_reconnect_attempts"] == 5
    assert server["reconnect_backoff_seconds"] == 1.5
    assert server["timeout_seconds"] == 12
    assert server["health_check_interval_seconds"] == 7

    assert (
        main(
            [
                "mcp",
                "set",
                "local",
                "--home",
                str(tmp_path),
                "--clear-inherit-env",
                "--clear-cwd",
                "--json",
            ]
        )
        == 0
    )
    cleared = json.loads(capsys.readouterr().out)["server"]
    assert cleared["inherit_env"] == []
    assert cleared["cwd"] is None


def test_mcp_cli_transport_switch_clears_incompatible_and_secret_fields(
    tmp_path,
    capsys,
) -> None:
    secret = "very-secret-token"
    assert (
        main(
            [
                "mcp",
                "set",
                "remote",
                "--home",
                str(tmp_path),
                "--transport",
                "http",
                "--url",
                "https://mcp.example.test/mcp",
                "--header",
                "X-Static=value",
                "--credential",
                f"Authorization=inline:{secret}",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert secret not in output
    assert json.loads(output)["server"]["credential_refs"] == {"Authorization": "inline:***"}
    assert secret in MCPConfigStore(tmp_path).config_path.read_text(encoding="utf-8")
    assert main(["mcp", "list", "--home", str(tmp_path), "--json"]) == 0
    assert secret not in capsys.readouterr().out
    assert main(["mcp", "show", "remote", "--home", str(tmp_path), "--json"]) == 0
    assert secret not in capsys.readouterr().out

    assert (
        main(
            [
                "mcp",
                "set",
                "remote",
                "--home",
                str(tmp_path),
                "--clear-headers",
                "--clear-credentials",
                "--json",
            ]
        )
        == 0
    )
    cleared = json.loads(capsys.readouterr().out)["server"]
    assert cleared["headers"] == {}
    assert cleared["credential_refs"] == {}

    assert (
        main(
            [
                "mcp",
                "set",
                "remote",
                "--home",
                str(tmp_path),
                "--header",
                "X-Static=value",
                "--credential",
                f"Authorization=inline:{secret}",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "mcp",
                "set",
                "remote",
                "--home",
                str(tmp_path),
                "--transport",
                "stdio",
                "--command",
                "python",
                "--json",
            ]
        )
        == 0
    )
    server = json.loads(capsys.readouterr().out)["server"]
    assert server["url"] is None
    assert server["headers"] == {}
    assert server["credential_refs"] == {}


def test_mcp_cli_reports_configuration_errors(tmp_path, capsys) -> None:
    code = main(["mcp", "set", "invalid", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "failed"
    assert "command is required" in payload["error"]

    code = main(["mcp", "show", "missing", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["error_type"] == "ConfigError"


def test_mcp_cli_check_connects_disabled_stdio_server(tmp_path, capsys) -> None:
    server_path = Path(__file__).parents[1] / "mcp" / "fixtures" / "echo_server.py"
    assert (
        main(
            [
                "mcp",
                "set",
                "echo",
                "--home",
                str(tmp_path),
                "--command",
                sys.executable,
                f"--arg={server_path}",
                "--arg=--transport",
                "--arg=stdio",
                "--no-enabled",
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = main(["mcp", "check", "echo", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["connection_status"] == "connected"
    assert payload["server"]["enabled"] is False
    assert payload["tools"] == ["echo"]
    assert payload["server_info"]["name"] == "OURO MCP integration test"
    persisted = MCPConfigStore(tmp_path).find_by_name("echo")
    assert persisted is not None
    assert persisted.enabled is False
    assert persisted.server_id not in json.dumps(payload)
    assert "server_id" not in json.dumps(payload)


def test_mcp_cli_check_failure_is_visible(tmp_path, capsys) -> None:
    missing = tmp_path / "missing_server.py"
    assert (
        main(
            [
                "mcp",
                "set",
                "broken",
                "--home",
                str(tmp_path),
                "--command",
                sys.executable,
                "--arg",
                str(missing),
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = main(["mcp", "check", "broken", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["status"] == "failed"
    assert payload["connection_status"] == "failed"
    assert payload["error"]
    assert payload["diagnostics"]
    config = MCPConfigStore(tmp_path).find_by_name("broken")
    assert config is not None
    assert config.server_id not in json.dumps(payload)
    assert "server_id" not in json.dumps(payload)
