from __future__ import annotations

import pytest

from ouro.core.context import ContextBuilder
from ouro.core.messages import MessageRecord


def test_context_builder_rejects_non_positive_token_budget() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ContextBuilder().build(messages=[], tools=[], input_token_budget=0)


def test_context_builder_preserves_all_messages_and_reports_hard_overflow() -> None:
    messages = [
        MessageRecord.text(session_id="ses_1", role="user", text="old " * 100),
        MessageRecord.text(session_id="ses_1", role="assistant", text="old answer"),
        MessageRecord.text(session_id="ses_1", role="user", text="latest"),
    ]

    context = ContextBuilder().build(
        messages=messages,
        tools=[],
        input_token_budget=20,
    )

    assert context.model_messages == messages
    assert context.context_metadata["over_budget"] is True
    assert context.context_metadata["estimated_input_tokens"] > 20
    assert "truncated" not in context.context_metadata
    assert "omitted_message_count" not in context.context_metadata


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


def test_context_builder_counts_synthetic_context_as_fixed_tokens() -> None:
    without_context = ContextBuilder().build(messages=[], tools=[], input_token_budget=100)
    with_context = ContextBuilder().build(
        messages=[],
        tools=[],
        added_context=["dynamic context"],
        input_token_budget=100,
    )

    assert (
        with_context.context_metadata["estimated_fixed_tokens"]
        > without_context.context_metadata["estimated_fixed_tokens"]
    )
