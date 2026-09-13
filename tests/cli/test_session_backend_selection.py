from __future__ import annotations

import json

from ouro.cli.main import main


def test_cli_selects_sqlite_without_mixing_file_sessions(tmp_path, capsys) -> None:
    run_code = main(
        [
            "run",
            "--json",
            "--home",
            str(tmp_path),
            "--session-backend",
            "sqlite",
            "hello",
        ]
    )
    run_payload = json.loads(capsys.readouterr().out)

    sqlite_code = main(
        [
            "session",
            "list",
            "--home",
            str(tmp_path),
            "--session-backend",
            "sqlite",
            "--json",
        ]
    )
    sqlite_payload = json.loads(capsys.readouterr().out)

    files_code = main(
        [
            "session",
            "list",
            "--home",
            str(tmp_path),
            "--session-backend",
            "files",
            "--json",
        ]
    )
    files_payload = json.loads(capsys.readouterr().out)

    assert run_code == sqlite_code == files_code == 0
    assert (tmp_path / "sessions.sqlite3").is_file()
    assert run_payload["session_id"] in {
        session["session_id"] for session in sqlite_payload["sessions"]
    }
    assert files_payload["sessions"] == []


def test_environment_default_can_be_overridden_by_global_cli_option(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OURO_SESSION_BACKEND", "sqlite")

    assert main(["run", "--json", "--home", str(tmp_path), "hello"]) == 0
    sqlite_run = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "--session-backend",
                "files",
                "session",
                "list",
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    files_payload = json.loads(capsys.readouterr().out)

    assert sqlite_run["session_id"].startswith("ses_")
    assert files_payload["sessions"] == []


def test_resource_commands_use_selected_session_backend(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "run",
                "--json",
                "--home",
                str(tmp_path),
                "--session-backend",
                "sqlite",
                "hello",
            ]
        )
        == 0
    )
    session_id = json.loads(capsys.readouterr().out)["session_id"]
    source = tmp_path / "source.txt"
    source.write_text("resource content", encoding="utf-8")

    assert (
        main(
            [
                "resource",
                "import",
                session_id,
                str(source),
                "--home",
                str(tmp_path),
                "--session-backend",
                "sqlite",
                "--json",
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "resource",
                "list",
                session_id,
                "--home",
                str(tmp_path),
                "--session-backend",
                "sqlite",
                "--json",
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)

    assert imported["resource"]["resource_id"] in {
        resource["resource_id"] for resource in listed["resources"]
    }


def test_invalid_environment_backend_is_reported_as_configuration_error(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OURO_SESSION_BACKEND", "remote")

    code = main(["session", "list", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    assert payload["error_type"] == "ConfigError"
    assert "OURO_SESSION_BACKEND" in payload["error"]


def test_approval_host_receives_selected_backend(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_approve(session_id: str, **kwargs) -> int:
        captured.update({"session_id": session_id, **kwargs})
        return 0

    monkeypatch.setattr("ouro.cli.main.approve_tool_command", fake_approve)

    assert main(["approve", "ses_1", "--session-backend", "sqlite"]) == 0
    assert captured["session_id"] == "ses_1"
    assert captured["session_backend"] == "sqlite"
