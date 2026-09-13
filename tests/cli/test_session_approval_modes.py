from __future__ import annotations

import json

from ouro.cli.main import main
from ouro.cli.parser import build_parser
from ouro.sessions import LocalSessionStore


def test_run_parser_accepts_session_approval_mode() -> None:
    args = build_parser().parse_args(["run", "--approval-mode", "deny", "hello"])

    assert args.approval_mode == "deny"


def test_run_parser_accepts_afk_preset() -> None:
    args = build_parser().parse_args(["run", "--afk", "hello"])

    assert args.afk is True
    assert args.approval_mode is None


def test_run_cli_persists_session_approval_mode(tmp_path, capsys) -> None:
    code = main(
        [
            "run",
            "--json",
            "--home",
            str(tmp_path),
            "--approval-mode",
            "auto",
            "hello",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["approval_mode"] == "auto"
    store = LocalSessionStore(tmp_path)
    session = store.load_session(payload["session_id"]).session
    assert session.approval_mode == "auto"


def test_run_cli_persists_afk_policy_pair(tmp_path, capsys) -> None:
    code = main(["run", "--json", "--home", str(tmp_path), "--afk", "hello"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["approval_mode"] == "auto"
    assert payload["interaction_mode"] == "unattended"
    store = LocalSessionStore(tmp_path)
    session = store.load_session_record(payload["session_id"])
    assert session.approval_mode == "auto"
    assert session.interaction_mode == "unattended"


def test_session_approval_mode_command_updates_existing_session(tmp_path, capsys) -> None:
    assert main(["run", "--json", "--home", str(tmp_path), "hello"]) == 0
    session_id = json.loads(capsys.readouterr().out)["session_id"]

    code = main(
        [
            "session",
            "approval-mode",
            session_id,
            "deny",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["session"]["approval_mode"] == "deny"


def test_session_afk_command_updates_existing_session_atomically(tmp_path, capsys) -> None:
    assert main(["run", "--json", "--home", str(tmp_path), "hello"]) == 0
    session_id = json.loads(capsys.readouterr().out)["session_id"]

    code = main(
        [
            "session",
            "afk",
            session_id,
            "on",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["session"]["approval_mode"] == "auto"
    assert payload["session"]["interaction_mode"] == "unattended"
