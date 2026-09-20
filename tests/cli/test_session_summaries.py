import json

import pytest

from ouro.cli.main import main
from ouro.local_application import build_local_session_services
from tests.session_summaries import add_session_summaries


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_summary_command_queries_all_saved_summaries(tmp_path, capsys, backend, json_mode):
    services = build_local_session_services(tmp_path, session_backend=backend)
    session = services.sessions.create(current_model="deleted/model").session
    records = add_session_summaries(services._store, session.session_id)
    args = [
        "session",
        "summary",
        session.session_id,
        "--home",
        str(tmp_path),
        "--session-backend",
        backend,
    ]
    before = services._store.load_session(session.session_id)

    assert main([*args, *(["--json"] if json_mode else [])]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if json_mode:
        payload = json.loads(captured.out)
        assert payload["status"] == "ok"
        assert payload["session_id"] == session.session_id
        assert [item["summary"] for item in payload["summaries"]] == [r.summary for r in records]
        assert [item["active"] for item in payload["summaries"]] == [False, True]
    else:
        assert session.session_id in captured.out
        assert f"#1 {records[0].compaction_id} [inactive]" in captured.out
        assert f"#2 {records[1].compaction_id} [active]" in captured.out
        assert all(record.summary in captured.out for record in records)
    assert services._store.load_session(session.session_id) == before


@pytest.mark.parametrize("json_mode", [False, True])
def test_summary_command_reports_empty_and_missing_sessions(tmp_path, capsys, json_mode):
    services = build_local_session_services(tmp_path)
    session = services.sessions.create().session
    options = ["--home", str(tmp_path), *(["--json"] if json_mode else [])]

    assert main(["session", "summary", session.session_id, *options]) == 0
    captured = capsys.readouterr()
    if json_mode:
        assert json.loads(captured.out)["summaries"] == []
    else:
        assert captured.out == f"No summaries for session {session.session_id}.\n"

    assert main(["session", "summary", "ses_missing", *options]) == 1
    captured = capsys.readouterr()
    if json_mode:
        payload = json.loads(captured.out)
        assert payload["status"] == "failed"
        assert payload["error_type"] == "SessionNotFoundError"
        assert captured.err == ""
    else:
        assert "session not found" in captured.err
        assert captured.out == ""
