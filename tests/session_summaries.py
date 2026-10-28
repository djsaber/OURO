"""Canonical conversation with two retained compaction summaries for Host tests."""

from datetime import UTC, datetime, timedelta

from ouro.core.compaction import CompactionRecord
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit
from ouro.sessions import SessionStore


def commit_summary(store: SessionStore, record: CompactionRecord) -> None:
    with store.session_mutation(record.session_id):
        store.commit_session(
            SessionCommit(before=store.load_session_record(record.session_id), compaction=record)
        )


def select_summary(store: SessionStore, session_id: str, compaction_id: str) -> None:
    session = store.load_session_record(session_id)
    store.save_session(
        session.model_copy(
            update={
                "context_selection": session.context_selection.model_copy(
                    update={"compaction_id": compaction_id}
                )
            }
        )
    )


def add_session_summaries(store: SessionStore, session_id: str) -> tuple[CompactionRecord, ...]:
    messages = [
        MessageRecord.text(
            session_id=session_id,
            role="user" if index % 2 else "assistant",
            text=f"Conversation message {index}",
            sequence=index,
        )
        for index in range(1, 6)
    ]
    for message in messages:
        store.append_message(message)
    records: list[CompactionRecord] = []
    for index, text in enumerate(
        ("First summary\n[red]literal[/red]", "第二次摘要\nRetained facts.")
    ):
        covered = messages[: (index + 1) * 2]
        record = CompactionRecord(
            session_id=session_id,
            previous_compaction_id=records[-1].compaction_id if records else None,
            strategy_id="test",
            trigger="auto" if index else "manual",
            summary=text,
            created_at=datetime(2026, 9, 20, tzinfo=UTC) + timedelta(minutes=index),
            covered_message_count=len(covered),
            covered_end_message_id=covered[-1].message_id,
            history_boundary_message_id=messages[len(covered)].message_id,
        )
        commit_summary(store, record)
        records.append(record)
    return tuple(records)
