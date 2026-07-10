from __future__ import annotations

import json

import pytest

from ouro.cli.main import main
from ouro.config.permissions import PermissionConfigManager


def test_config_permissions_default_json(tmp_path, capsys) -> None:
    code = main(["config", "permissions", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["tools"] == {}


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
