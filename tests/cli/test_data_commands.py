from __future__ import annotations

import json

import pytest

from ouro.cli.main import main
from ouro.config.providers import ProviderConfigManager
from ouro.local_application import build_local_session_services


@pytest.mark.parametrize(
    "kind", ["providers", "provider-templates", "mcp", "agents", "sessions", "skills"]
)
def test_unified_data_command_round_trip(tmp_path, capsys, kind):
    source, target = tmp_path / "source", tmp_path / "target"
    archive = tmp_path / "export with spaces.zip"
    for action, home in (("export", source), ("import", target)):
        assert (
            main(
                [
                    "data",
                    action,
                    "--kind",
                    kind,
                    "--home",
                    str(home),
                    "--path",
                    str(archive),
                    "--json",
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "ok"
        assert result["kind"] == kind
        assert result["action"] == action
    if kind not in {"skills", "sessions"}:
        assert not (target / "sessions").exists()
        assert not (target / "sessions.sqlite3").exists()


def test_data_import_cli_selects_several_sessions_from_full_archive(tmp_path, capsys):
    source = tmp_path / "source"
    sessions = build_local_session_services(source).sessions
    for identity in ("ses_one", "ses_two", "ses_three"):
        sessions.create(session_id=identity)
    archive = tmp_path / "all.zip"
    assert (
        main(
            ["data", "export", "--kind", "sessions", "--home", str(source), "--path", str(archive)]
        )
        == 0
    )
    assert (
        main(
            [
                "data",
                "import",
                "--kind",
                "sessions",
                "--home",
                str(tmp_path / "target"),
                "--path",
                str(archive),
                "--id",
                "ses_one",
                "--id",
                "ses_three",
                "--session-backend",
                "sqlite",
            ]
        )
        == 0
    )
    actual = build_local_session_services(tmp_path / "target", session_backend="sqlite")
    assert {session.session_id for session in actual._store.list_sessions()} == {
        "ses_one",
        "ses_three",
    }
    assert "Imported sessions: 2" in capsys.readouterr().out


def test_data_cli_failure_is_json_and_does_not_overwrite_configuration(tmp_path, capsys):
    providers = ProviderConfigManager(tmp_path)
    providers.create_provider("example", provider_type="custom", protocol="openai-chat-completions")
    before = providers.config_path.read_bytes()
    assert (
        main(
            [
                "data",
                "import",
                "--kind",
                "providers",
                "--home",
                str(tmp_path),
                "--path",
                str(tmp_path / "missing.zip"),
                "--replace",
                "--json",
            ]
        )
        != 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["error_type"] == "DataTransferError"
    assert providers.config_path.read_bytes() == before
