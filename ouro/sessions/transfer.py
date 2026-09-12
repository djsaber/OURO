from __future__ import annotations

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.errors import PersistenceError
from ouro.core.resources import SessionResourceReference
from ouro.sessions.base import SessionRepository, SessionSnapshot
from ouro.sessions.sqlite import SQLiteSessionStore


class SQLiteImportReport(OuroModel):
    """Verified result of one atomic import into a SQLite Session store."""

    session_ids: list[str] = Field(default_factory=list)
    message_count: int = 0
    event_count: int = 0
    run_count: int = 0
    compaction_count: int = 0
    resource_reference_count: int = 0
    extension_state_count: int = 0
    delegation_count: int = 0


def import_sessions_to_sqlite(
    source: SessionRepository,
    target: SQLiteSessionStore,
) -> SQLiteImportReport:
    """Copy all active Sessions into an empty SQLite store and verify before commit.

    Each source Session is read under its canonical mutation lock. The complete
    target import runs in one SQLite transaction, so any copy or verification
    failure leaves no partially imported Sessions behind.
    """

    target.initialize()
    source_sessions = sorted(source.list_sessions(), key=lambda item: item.session_id)
    report = SQLiteImportReport()

    try:
        with target.session_mutation("import"):
            if target.list_sessions():
                raise PersistenceError("SQLite import target must contain no active sessions")

            for session in source_sessions:
                with source.session_mutation(session.session_id):
                    snapshot = source.load_session(session.session_id)
                    references = source.load_resource_references(session.session_id)
                _copy_snapshot(target, snapshot, references)
                _verify_snapshot(target, snapshot, references)
                report = report.model_copy(
                    update={
                        "session_ids": [*report.session_ids, session.session_id],
                        "message_count": report.message_count + len(snapshot.messages),
                        "event_count": report.event_count + len(snapshot.events),
                        "run_count": report.run_count + len(snapshot.runs),
                        "compaction_count": report.compaction_count + len(snapshot.compactions),
                        "resource_reference_count": report.resource_reference_count
                        + len(references),
                        "extension_state_count": report.extension_state_count
                        + len(snapshot.extension_states),
                    }
                )
            root_ids = sorted(
                {
                    session.agent_root_session_id
                    for session in source_sessions
                    if session.session_kind == "primary"
                }
            )
            for root_id in root_ids:
                with source.session_mutation(root_id):
                    expected = source.list_delegations(root_id)
                for delegation in expected:
                    target.create_delegation(delegation)
                actual = target.list_delegations(root_id)
                if actual != expected:
                    raise PersistenceError(
                        f"SQLite import verification failed for root Delegations {root_id}"
                    )
                report = report.model_copy(
                    update={"delegation_count": report.delegation_count + len(expected)}
                )
            # Import is an explicit maintenance boundary. Publish the complete
            # disposable projection in the same all-or-nothing target transaction.
            target.rebuild_catalog()
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError(f"SQLite Session import failed and was rolled back: {exc}") from exc

    return report


def _copy_snapshot(
    target: SQLiteSessionStore,
    snapshot: SessionSnapshot,
    references: list[SessionResourceReference],
) -> None:
    target.create_session_snapshot(
        snapshot.session,
        snapshot.messages,
        resource_references=references,
        harness_versions=snapshot.harness_versions,
        commits=snapshot.commits,
        compactions=snapshot.compactions,
    )
    for run in snapshot.runs:
        target.save_run(run)
    for event in snapshot.events:
        target.append_event(event)


def _verify_snapshot(
    target: SQLiteSessionStore,
    expected: SessionSnapshot,
    expected_references: list[SessionResourceReference],
) -> None:
    actual = target.load_session(expected.session.session_id)
    actual_references = target.load_resource_references(expected.session.session_id)
    if actual != expected or _sorted_references(actual_references) != _sorted_references(
        expected_references
    ):
        raise PersistenceError(
            f"SQLite import verification failed for session {expected.session.session_id}"
        )


def _sorted_references(
    references: list[SessionResourceReference],
) -> list[SessionResourceReference]:
    return sorted(references, key=lambda item: item.resource_id)
