from __future__ import annotations

import json

import pytest

from ouro.application.data_transfer import DataTransferRequest
from ouro.cli.main import main
from ouro.config.prompts import PromptConfigManager, PromptDraft
from ouro.config.providers import ProviderConfigManager
from ouro.local_application import build_local_data_transfer_service, build_local_session_services


@pytest.mark.parametrize(
    "kind",
    ["providers", "provider-templates", "mcp", "agents", "prompts", "sessions", "skills"],
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


def test_data_cli_round_trips_personal_prompts_without_restart_notice(tmp_path, capsys):
    source, target = tmp_path / "source", tmp_path / "target"
    PromptConfigManager(source).save_entry(
        "replacement",
        PromptDraft(description="Portable", text="Use this system prompt", enabled=True),
        expected_revision=0,
    )
    archive = tmp_path / "prompts.zip"

    for action, home in (("export", source), ("import", target)):
        assert (
            main(
                [
                    "data",
                    action,
                    "--kind",
                    "prompts",
                    "--home",
                    str(home),
                    "--path",
                    str(archive),
                ]
            )
            == 0
        )
        output = capsys.readouterr().out
        assert f"{action.title()}ed prompts: 1 configuration" in output
        assert "Restart running OURO hosts" not in output

    imported = PromptConfigManager(target).snapshot().config
    assert imported.system_prompt == "Use this system prompt"
    assert imported.replacements[0].description == "Portable"


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


@pytest.mark.parametrize("flag", ["--skip-existing", "--replace"])
def test_session_import_cli_conflict_policy(tmp_path, capsys, flag):
    source, target = tmp_path / "source", tmp_path / "target"
    build_local_session_services(source).sessions.create(session_id="ses_one", title="Incoming")
    build_local_session_services(target).sessions.create(session_id="ses_one", title="Original")
    archive = tmp_path / "sessions.zip"
    build_local_data_transfer_service(source).transfer(
        DataTransferRequest(action="export", kind="sessions", path=archive)
    )
    assert (
        main(
            [
                "data",
                "import",
                "--kind",
                "sessions",
                "--path",
                str(archive),
                "--home",
                str(target),
                flag,
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["skipped"] == (1 if flag == "--skip-existing" else 0)
    assert result["replaced"] == (1 if flag == "--replace" else 0)
    assert build_local_session_services(target)._store.load_session_record("ses_one").title == (
        "Incoming" if flag == "--replace" else "Original"
    )
