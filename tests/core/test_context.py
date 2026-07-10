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
    assert context.context_metadata["truncated"] is True
    assert context.context_metadata["omitted_message_count"] == 3
    assert context.context_metadata["skills_injected"] is False
    assert context.context_metadata["estimated_input_tokens"] > 0
    assert context.context_metadata["input_token_budget"] is None
    assert context.context_metadata["over_budget"] is False


def test_context_builder_places_skills_in_system_prompt_and_keeps_dynamic_context() -> None:
    context = ContextBuilder(skill_instructions="skills").build(
        messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
        tools=[],
        system_prompt="system",
        added_context=["plugin"],
    )

    assert context.system_prompt == "system\n\nskills"
    assert [message.text_content for message in context.model_messages] == ["plugin", "hello"]
    assert context.context_metadata["skills_injected"] is True


def test_context_builder_preserves_native_system_prompt_when_no_skills_are_present() -> None:
    system_prompt = "  Preserve this prompt exactly.  \n"

    context = ContextBuilder().build(
        messages=[],
        tools=[],
        system_prompt=system_prompt,
    )

    assert context.system_prompt == system_prompt


def test_context_builder_honors_token_budget_without_splitting_tool_exchange() -> None:
    messages = [
        MessageRecord.text(session_id="ses_1", role="user", text="old " * 100),
        MessageRecord.text(session_id="ses_1", role="assistant", text="old answer"),
        MessageRecord.text(session_id="ses_1", role="user", text="read it"),
        MessageRecord.text(session_id="ses_1", role="assistant", text="").model_copy(
            update={
                "metadata": {
                    "tool_calls": [{"id": "call_1", "name": "Read", "arguments": {"path": "a"}}]
                }
            }
        ),
        MessageRecord.text(session_id="ses_1", role="tool_result", text="result").model_copy(
            update={"metadata": {"tool_call_id": "call_1", "tool_name": "Read"}}
        ),
    ]

    context = ContextBuilder(max_messages=40).build(
        messages=messages,
        tools=[],
        input_token_budget=80,
    )

    assert [message.role for message in context.model_messages] == [
        "assistant",
        "tool_result",
    ]
    assert context.context_metadata["truncated"] is True
    assert context.context_metadata["omitted_message_count"] == 3
    assert context.context_metadata["input_token_budget"] == 80
