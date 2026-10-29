from __future__ import annotations

import sqlite3

from ouro.core.errors import PersistenceError
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.state import RunRecord, RunRequestCounts


def read_run(connection: sqlite3.Connection, session_id: str, run_id: str) -> RunRecord:
    row = connection.execute(
        "SELECT payload FROM runs WHERE session_id = ? AND run_id = ?", (session_id, run_id)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(run_id)
    return _decode_run(row[0], session_id, run_id)


def read_pending_runs(
    connection: sqlite3.Connection, session_id: str, history_epoch: int
) -> list[RunRecord]:
    rows = connection.execute(
        "SELECT run_id, payload FROM runs WHERE session_id = ? AND history_epoch = ? "
        "AND needs_recovery = 1 ORDER BY started_at, run_id",
        (session_id, history_epoch),
    )
    runs = [_decode_run(row[1], session_id, row[0]) for row in rows]
    if any(run.history_epoch != history_epoch or not run.needs_recovery for run in runs):
        raise PersistenceError("pending Run index disagrees with its Run")
    return runs


def read_runs(connection: sqlite3.Connection, session_id: str) -> list[RunRecord]:
    return [
        _decode_run(row[1], session_id, row[0])
        for row in connection.execute(
            "SELECT run_id, payload FROM runs WHERE session_id = ? ORDER BY started_at, run_id",
            (session_id,),
        )
    ]


def _decode_run(payload: str, session_id: str, run_id: str) -> RunRecord:
    run = decode_versioned_record_json(RunRecord, payload, label=f"run {run_id}")
    if run.session_id != session_id or run.run_id != run_id:
        raise PersistenceError("Run record has a foreign identity")
    return run


def write_run(
    connection: sqlite3.Connection, run: RunRecord, *, history_epoch: int, revision: int = 0
) -> bool:
    added = (
        connection.execute(
            "SELECT 1 FROM runs WHERE session_id = ? AND run_id = ?", (run.session_id, run.run_id)
        ).fetchone()
        is None
    )
    connection.execute(
        "INSERT INTO runs(session_id, run_id, started_at, history_epoch, needs_recovery, "
        "model_round_count, summary_request_count, payload, modified_revision) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, run_id) DO UPDATE SET "
        "started_at = excluded.started_at, history_epoch = excluded.history_epoch, "
        "model_round_count = excluded.model_round_count, "
        "summary_request_count = excluded.summary_request_count, "
        "needs_recovery = excluded.needs_recovery, payload = excluded.payload, "
        "modified_revision = excluded.modified_revision",
        (
            run.session_id,
            run.run_id,
            str(run.started_at),
            run.history_epoch,
            int(run.needs_recovery and run.history_epoch == history_epoch),
            run.model_round_count,
            run.summary_request_count,
            run.model_dump_json(),
            revision,
        ),
    )
    return added


def read_request_counts(
    connection: sqlite3.Connection, session_id: str
) -> tuple[RunRequestCounts, ...]:
    return tuple(
        RunRequestCounts(run_id=row[0], model_round_count=row[1], summary_request_count=row[2])
        for row in connection.execute(
            "SELECT run_id, model_round_count, summary_request_count FROM runs "
            "WHERE session_id = ? ORDER BY run_id",
            (session_id,),
        )
    )
