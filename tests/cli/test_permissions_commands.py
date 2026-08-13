from __future__ import annotations

import json

import pytest

from ouro.cli.main import main
from ouro.config.permissions import PermissionConfigManager
from ouro.core.errors import ConfigError


def test_config_permissions_default_json(tmp_path, capsys) -> None:
    code = main(["config", "permissions", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["tools"] == {}
    assert payload["plugins"] == {}


def test_config_permissions_set_tool(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "permissions",
            "--tool",
            "Write",
            "--mode",
            "approval",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["tools"] == {"Write": "approval"}

    manager = PermissionConfigManager(tmp_path)
    snapshot = manager.snapshot()
    assert snapshot.tools == {"Write": "approval"}


def test_config_permissions_invalid_mode(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "config",
                "permissions",
                "--tool",
                "Write",
                "--mode",
                "invalid",
                "--home",
                str(tmp_path),
            ]
        )

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err.lower()


def test_config_permissions_human_readable(tmp_path, capsys) -> None:
    main(
        ["config", "permissions", "--tool", "Write", "--mode", "approval", "--home", str(tmp_path)]
    )
    captured = capsys.readouterr()

    assert "Write" in captured.out
    assert "approval" in captured.out


def test_config_permissions_human_readable_includes_plugin_defaults(tmp_path, capsys) -> None:
    PermissionConfigManager(tmp_path).set_plugin_mode("dev.ouro.deploy", "deny")

    code = main(["config", "permissions", "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "Plugin tool permission defaults" in captured.out
    assert "dev.ouro.deploy: deny" in captured.out


def test_config_permissions_supports_hard_deny(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "permissions",
            "--tool",
            "Bash",
            "--mode",
            "deny",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["tools"]["Bash"] == "deny"


@pytest.mark.parametrize(
    "permissions",
    ["approval", {"tools": []}, {"tools": {"Write": "sometimes"}}],
)
def test_permission_config_rejects_invalid_persisted_shape(tmp_path, permissions) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"permissions": permissions}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="permissions config|invalid permissions config"):
        PermissionConfigManager(tmp_path).snapshot()
