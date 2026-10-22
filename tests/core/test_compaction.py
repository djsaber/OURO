from __future__ import annotations

import pytest
from pydantic import ValidationError

from ouro.core.compaction import (
    CompactionArtifact,
    CompactionRecord,
    CompactionStrategyRegistry,
    compaction_frames,
    compaction_source_digest,
    conversation_turns,
    plan_compaction,
    project_compacted_messages,
)
from ouro.core.errors import CompactionStateError
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.tools import ToolCallRequest


def _message(role, text, sequence):
    return MessageRecord.text(
        session_id="ses_1",
        role=role,
        text=text,
        sequence=sequence,
    )


def test_compaction_plan_preserves_complete_tool_exchange_boundaries() -> None:
    messages = [
        _message("user", "old", 1),
        _message("assistant", "", 2).model_copy(
            update={"tool_calls": [ToolCallRequest(id="call_1", name="Read")]}
        ),
        _message("tool_result", "result", 3).model_copy(
            update={"tool_result": ToolResultReference(tool_call_id="call_1", tool_name="Read")}
        ),
        _message("assistant", "done", 4),
        _message("user", "latest", 5),
        _message("assistant", "working", 6),
    ]

    plan = plan_compaction(messages, tail_token_budget=1, force=True)

    assert plan.messages_to_compact == messages[:4]
    assert plan.retained_messages == messages[4:]
    assert plan.messages_to_compact + plan.retained_messages == messages
    assert conversation_turns(messages) == [messages[:4], messages[4:]]


def test_compaction_plan_uses_model_turn_ids_inside_one_long_user_task() -> None:
    user = _message("user", "refactor the project", 1).model_copy(update={"run_id": "run_1"})
    first_assistant = _message("assistant", "", 2).model_copy(
        update={
            "run_id": "run_1",
            "turn_id": "turn_1",
            "tool_calls": [ToolCallRequest(id="call_1", name="Read")],
        }
    )
    first_result = _message("tool_result", "old result " + ("x" * 200), 3).model_copy(
        update={
            "run_id": "run_1",
            "turn_id": "turn_1",
            "tool_result": ToolResultReference(tool_call_id="call_1", tool_name="Read"),
        }
    )
    second_assistant = _message("assistant", "", 4).model_copy(
        update={
            "run_id": "run_1",
            "turn_id": "turn_2",
            "tool_calls": [ToolCallRequest(id="call_2", name="Edit")],
        }
    )
    second_result = _message("tool_result", "recent result", 5).model_copy(
        update={
            "run_id": "run_1",
            "turn_id": "turn_2",
            "tool_result": ToolResultReference(tool_call_id="call_2", tool_name="Edit"),
        }
    )
    messages = [user, first_assistant, first_result, second_assistant, second_result]

    plan = plan_compaction(messages, tail_token_budget=1)

    assert compaction_frames(messages) == [
        [user, first_assistant, first_result],
        [second_assistant, second_result],
    ]
    assert plan.messages_to_compact == messages[:3]
    assert plan.retained_messages == messages[3:]


def test_compaction_plan_can_cover_history_and_earlier_frames_of_the_same_run() -> None:
    old = [
        _message("user", "old request", 1).model_copy(update={"run_id": "run_old"}),
        _message("assistant", "old answer", 2).model_copy(update={"run_id": "run_old"}),
    ]
    active = [
        _message("user", "skill request", 3).model_copy(update={"run_id": "run_active"}),
        _message("assistant", "first round", 4).model_copy(
            update={"run_id": "run_active", "turn_id": "turn_1"}
        ),
        _message("assistant", "second round", 5).model_copy(
            update={"run_id": "run_active", "turn_id": "turn_2"}
        ),
    ]

    plan = plan_compaction(
        [*old, *active],
        tail_token_budget=1,
    )

    assert plan.messages_to_compact == [*old, *active[:2]]
    assert plan.retained_messages == active[2:]


@pytest.mark.parametrize("force", [False, True])
def test_compaction_preserves_the_suffix_from_an_unfinished_tool_exchange(force) -> None:
    old = _message("assistant", "completed", 1).model_copy(
        update={"run_id": "run_1", "turn_id": "turn_1"}
    )
    pending = _message("assistant", "", 2).model_copy(
        update={
            "run_id": "run_1",
            "turn_id": "turn_2",
            "tool_calls": [ToolCallRequest(id="call_pending", name="Read")],
        }
    )
    later = _message("assistant", "later frame", 3).model_copy(
        update={"run_id": "run_1", "turn_id": "turn_3"}
    )

    plan = plan_compaction([old, pending, later], tail_token_budget=0, force=force)

    assert plan.messages_to_compact == [old]
    assert plan.retained_messages == [pending, later]


def test_compaction_frames_keep_unidentified_tool_exchange_atomic() -> None:
    assistant = _message("assistant", "", 1).model_copy(
        update={"tool_calls": [ToolCallRequest(id="call_1", name="Read")]}
    )
    result = _message("tool_result", "result", 2).model_copy(
        update={"tool_result": ToolResultReference(tool_call_id="call_1", tool_name="Read")}
    )

    assert compaction_frames([assistant, result]) == [[assistant, result]]


def test_compaction_frames_attach_user_input_to_following_identified_turn() -> None:
    user = _message("user", "request", 1)
    assistant = _message("assistant", "answer", 2).model_copy(
        update={"run_id": "run_1", "turn_id": "turn_1"}
    )

    assert compaction_frames([user, assistant]) == [[user, assistant]]


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
        history_boundary_message_id=messages[-1].message_id,
    )

    projected = project_compacted_messages(messages, record)

    assert [message.text_content for message in messages] == ["old", "answer", "latest"]
    assert [message.text_content for message in projected] == ["summary", "latest"]
    assert projected[0].role == "system"
    assert projected[0].scaffold is True
    assert projected[0].semantic_tag == "conversation-summary"
    assert projected[0].metadata["compaction_id"] == record.compaction_id


def test_compaction_record_requires_an_explicit_history_boundary() -> None:
    with pytest.raises(ValidationError, match="history_boundary_message_id"):
        CompactionRecord(
            session_id="ses_1",
            strategy_id="test.summary",
            summary="summary",
            trigger="manual",
            source_digest="sha256:" + "0" * 64,
        )


def test_compaction_projection_rejects_history_lineage_mismatch() -> None:
    messages = [_message("user", "changed", 1)]
    record = CompactionRecord(
        session_id="ses_1",
        strategy_id="test.summary",
        summary="summary",
        trigger="auto",
        covered_message_ids=["msg_missing"],
        source_digest="invalid",
        history_boundary_message_id=messages[-1].message_id,
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
