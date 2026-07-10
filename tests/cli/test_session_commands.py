from __future__ import annotations

import json

from ouro.cli.main import main
from ouro.sessions import LocalSessionStore


def test_session_list_shows_new_session(tmp_path, capsys) -> None:
    assert main(["run", "--print", "--home", str(tmp_path), "hello"]) == 0

    code = main(["session", "list", "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "Sessions:" in captured.out


def test_session_list_json(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    code = main(["session", "list", "--home", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ok"
    session_ids = [session["session_id"] for session in payload["sessions"]]
    assert run_payload["session_id"] in session_ids


def test_session_show(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    code = main(["session", "show", run_payload["session_id"], "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert run_payload["session_id"] in captured.out


def test_session_show_missing(tmp_path, capsys) -> None:
    code = main(["session", "show", "ses_missing", "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "session not found" in captured.err


def test_session_remove_with_force(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    code = main(
        [
            "session",
            "remove",
            run_payload["session_id"],
            "--home",
            str(tmp_path),
            "--force",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "removed" in captured.out
    store = LocalSessionStore(tmp_path)
    assert not (store.sessions_dir / run_payload["session_id"]).exists()


def test_session_export_to_stdout(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    code = main(["session", "export", run_payload["session_id"], "--home", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["session"]["session_id"] == run_payload["session_id"]
    assert any(event["event_type"] == "run_completed" for event in payload["events"])


def test_session_export_to_file(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    output = tmp_path / "export.json"
    code = main(
        [
            "session",
            "export",
            run_payload["session_id"],
            "--home",
            str(tmp_path),
            "--output",
            str(output),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "Exported session" in captured.out
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["session"]["session_id"] == run_payload["session_id"]


def test_session_messages_and_events(tmp_path, capsys) -> None:
    run_code = main(["run", "--json", "--home", str(tmp_path), "hello"])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0

    messages_code = main(
        ["session", "messages", run_payload["session_id"], "--home", str(tmp_path), "--json"]
    )
    messages_payload = json.loads(capsys.readouterr().out)
    assert messages_code == 0
    assert any(message["role"] == "user" for message in messages_payload["messages"])

    events_code = main(
        ["session", "events", run_payload["session_id"], "--home", str(tmp_path), "--json"]
    )
    events_payload = json.loads(capsys.readouterr().out)
    assert events_code == 0
    assert any(event["event_type"] == "run_completed" for event in events_payload["events"])
