from __future__ import annotations

import pytest

from ouro.core.context import ContextBuilder
from ouro.core.messages import MessageRecord


def test_context_builder_rejects_non_positive_message_limit() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ContextBuilder(max_messages=0)


def test_context_builder_keeps_tool_request_with_leading_tool_results() -> None:
    messages = [
        MessageRecord.text(session_id="ses_1", role="user", text="old"),
        MessageRecord.text(session_id="ses_1", role="assistant", text="done"),
        MessageRecord.text(session_id="ses_1", role="user", text="read both"),
        MessageRecord.text(session_id="ses_1", role="assistant", text="").model_copy(
            update={
                "metadata": {
                    "tool_calls": [
                        {"id": "call_1", "name": "Read", "arguments": {"path": "a"}},
                        {"id": "call_2", "name": "Read", "arguments": {"path": "b"}},
                    ]
                }
            }
        ),
        MessageRecord.text(session_id="ses_1", role="tool_result", text="a").model_copy(
            update={"metadata": {"tool_call_id": "call_1", "tool_name": "Read"}}
        ),
        MessageRecord.text(session_id="ses_1", role="tool_result", text="b").model_copy(
            update={"metadata": {"tool_call_id": "call_2", "tool_name": "Read"}}
        ),
    ]

    context = ContextBuilder(max_messages=2).build(messages=messages, tools=[])

    assert [message.role for message in context.model_messages] == [
        "assistant",
        "tool_result",
        "tool_result",
    ]
    assert context.context_metadata == {"truncated": True, "omitted_message_count": 3}


def test_context_builder_places_base_context_before_dynamic_context() -> None:
    context = ContextBuilder(base_context=["skills"]).build(
        messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
        tools=[],
        added_context=["plugin"],
    )

    assert [message.text_content for message in context.model_messages] == [
        "skills",
        "plugin",
        "hello",
    ]
