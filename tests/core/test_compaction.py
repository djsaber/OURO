from __future__ import annotations

import pytest

from ouro.core.compaction import (
    CompactionArtifact,
    CompactionRecord,
    CompactionStrategyRegistry,
    compaction_source_digest,
    conversation_turns,
    plan_compaction,
    project_compacted_messages,
)
from ouro.core.errors import CompactionStateError
from ouro.core.messages import MessageRecord


def _message(role, text, sequence):
    return MessageRecord.text(
        session_id="ses_1",
        role=role,
        text=text,
        sequence=sequence,
    )


def test_compaction_plan_preserves_latest_turn_and_complete_tool_exchange() -> None:
    messages = [
        _message("user", "old", 1),
        _message("assistant", "", 2).model_copy(
            update={"metadata": {"tool_calls": [{"id": "call_1", "name": "Read"}]}}
        ),
        _message("tool_result", "result", 3).model_copy(
            update={"metadata": {"tool_call_id": "call_1"}}
        ),
        _message("assistant", "done", 4),
        _message("user", "latest", 5),
        _message("assistant", "working", 6),
    ]

    plan = plan_compaction(messages, None, tail_token_budget=1, force=True)

    assert plan.messages_to_compact == messages[:4]
    assert plan.retained_messages == messages[4:]
    assert plan.covered_message_ids == [message.message_id for message in messages[:4]]
    assert conversation_turns(messages) == [messages[:4], messages[4:]]


def test_compaction_projection_replaces_covered_prefix_without_mutating_history() -> None:
    messages = [
        _message("user", "old", 1),
        _message("assistant", "answer", 2),
        _message("user", "latest", 3),
    ]
    record = CompactionRecord(
        session_id="ses_1",
        strategy_id="test.summary",
        summary="summary",
        trigger="auto",
        covered_message_ids=[message.message_id for message in messages[:2]],
        source_digest=compaction_source_digest(messages[:2]),
    )

    projected = project_compacted_messages(messages, record)

    assert [message.text_content for message in messages] == ["old", "answer", "latest"]
    assert [message.text_content for message in projected] == ["summary", "latest"]
    assert projected[0].role == "system_synthetic"
    assert projected[0].metadata["compaction_id"] == record.compaction_id


def test_compaction_projection_rejects_history_lineage_mismatch() -> None:
    messages = [_message("user", "changed", 1)]
    record = CompactionRecord(
        session_id="ses_1",
        strategy_id="test.summary",
        summary="summary",
        trigger="auto",
        covered_message_ids=["msg_missing"],
        source_digest="invalid",
    )

    with pytest.raises(CompactionStateError, match="canonical message history"):
        project_compacted_messages(messages, record)


def test_compaction_strategy_registry_rejects_silent_replacement() -> None:
    class Strategy:
        strategy_id = "test.summary"
        version = "1"

        async def compact(self, request, *, signal=None):
            del request, signal
            return CompactionArtifact(summary="summary")

    registry = CompactionStrategyRegistry()
    registry.register(Strategy())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(Strategy())
