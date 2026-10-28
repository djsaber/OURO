from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def historical_home(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    backend = getattr(request, "param", "files")
    home = tmp_path / "home"
    shutil.copytree(FIXTURES / backend, home)
    for source in home.rglob("*.sql"):
        with closing(sqlite3.connect(source.with_suffix(".sqlite3"))) as connection:
            connection.executescript(source.read_text("utf-8"))
        source.unlink()

    # Compose a consistent tool history from the frozen writer output. The original
    # fixture deliberately used unrelated request/invocation/result identities.
    def normalize_run(raw):
        batch = raw.get("active_tool_batch")
        if batch:
            for call in batch["calls"]:
                if call.get("invocation"):
                    call["invocation"]["tool_call_id"] = call["request"]["id"]
                    call["invocation"]["turn_id"] = batch["turn_id"]
        return raw

    def normalize_messages(messages):
        call_id = messages[1]["tool_calls"][0]["id"]
        messages[-1]["tool_result"]["tool_call_id"] = call_id
        return messages

    if backend == "files":
        path = home / "sessions/ses_fixture/runs/run_fixture.json"
        path.write_text(json.dumps(normalize_run(json.loads(path.read_text("utf-8")))), "utf-8")
        path = home / "sessions/ses_fixture/messages.jsonl"
        messages = normalize_messages(
            [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        )
        path.write_text("\n".join(map(json.dumps, messages)) + "\n", "utf-8")
    else:
        with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
            raw = json.loads(connection.execute("SELECT payload FROM runs").fetchone()[0])
            connection.execute("UPDATE runs SET payload=?", (json.dumps(normalize_run(raw)),))
            messages = normalize_messages(
                [
                    json.loads(row[0])
                    for row in connection.execute("SELECT payload FROM messages ORDER BY sequence")
                ]
            )
            connection.execute(
                "UPDATE messages SET payload=? WHERE message_id=?",
                (json.dumps(messages[-1]), messages[-1]["message_id"]),
            )
            connection.commit()
    return home
