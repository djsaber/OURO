from __future__ import annotations

import asyncio

import pytest

from dev.benchmarks import session_terminal
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_seed_history_is_visible_in_an_existing_catalog(tmp_path, monkeypatch, backend):
    store = (
        LocalSessionStore(tmp_path) if backend == "files" else SQLiteSessionStore(tmp_path / "db")
    )
    store.initialize()
    store.rebuild_catalog()
    applications = []

    class CapturedApp:
        def __init__(self, *, application):
            applications.append(application)

        def run(self):
            pass

    monkeypatch.setattr(session_terminal, "OuroTuiApp", CapturedApp)
    monkeypatch.setattr(
        "sys.argv",
        [
            "session_terminal",
            "--home",
            str(tmp_path),
            "--backend",
            backend,
            "--seed-history",
            "10000",
        ],
    )
    session_terminal.main()
    try:
        entries = applications[0].runtime.sessions.catalog().entries
        assert len(entries) == 1
        assert entries[0].title == "Synthetic 10000 messages"
        assert entries[0].namespace == "default"
        assert entries[0].message_count == 10000
        assert store.load_session_record(entries[0].session_id).message_count == 10000
    finally:
        asyncio.run(applications[0].close())
