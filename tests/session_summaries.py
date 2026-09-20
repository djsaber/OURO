"""Canonical conversation with two retained compaction summaries for Host tests."""

from datetime import UTC, datetime, timedelta

from ouro.core.compaction import CompactionRecord, compaction_source_digest
from ouro.core.messages import MessageRecord
from ouro.sessions import SessionStore


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
            covered_message_ids=[message.message_id for message in covered],
            source_digest=compaction_source_digest(covered),
            history_boundary_message_id=messages[len(covered)].message_id,
        )
        store.append_compaction(record)
        store.activate_compaction(session_id, record.compaction_id)
        records.append(record)
    return tuple(records)
