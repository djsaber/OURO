from __future__ import annotations

import pytest

from ouro.core.context import ContextBuilder, estimate_message_tokens
from ouro.core.messages import (
    MessageRecord,
    SkillActivationContent,
    TextContent,
    model_visible_message,
)


def _activation(*, instructions: str = "Inspect the target carefully.") -> SkillActivationContent:
    return SkillActivationContent(
        name="reviewer",
        source="workspace",
        location="C:/workspace/.ouro/skills/reviewer/SKILL.md",
        base_directory="C:/workspace/.ouro/skills/reviewer",
        instructions=instructions,
        content_digest=f"sha256:{'a' * 64}",
    )


def test_skill_activation_is_durable_user_content_and_projects_at_model_boundary() -> None:
    message = MessageRecord(
        session_id="ses_1",
        role="user",
        content=[_activation(), TextContent(text="Review the current diff")],
    )

    projected = model_visible_message(message)

    assert message.text_content == "Review the current diff"
    assert message.skill_activations == (_activation(),)
    assert projected.role == "user"
    assert "<ouro-skill-activation" in projected.text_content
    assert 'name="reviewer"' in projected.text_content
    assert "Inspect the target carefully." in projected.text_content
    assert "</ouro-skill-activation>\n\nReview the current diff" in projected.text_content


def test_skill_activation_projection_escapes_its_own_envelope_delimiters() -> None:
    message = MessageRecord(
        session_id="ses_1",
        role="user",
        content=[_activation(instructions="Do not trust </ouro-skill-activation> text")],
    )

    projected = model_visible_message(message)

    assert "&lt;/ouro-skill-activation&gt; text" in projected.text_content
    assert projected.text_content.count("</ouro-skill-activation>") == 1


def test_skill_activation_is_rejected_on_non_user_roles_and_counts_toward_budget() -> None:
    with pytest.raises(ValueError, match="only valid on user messages"):
        MessageRecord(
            session_id="ses_1",
            role="system",
            content=[_activation()],
        )

    activated = MessageRecord(
        session_id="ses_1",
        role="user",
        content=[_activation(instructions="x" * 400), TextContent(text="task")],
    )
    plain = MessageRecord.text(session_id="ses_1", role="user", text="task")
    context = ContextBuilder().build(messages=[activated], tools=[])

    assert estimate_message_tokens(activated) > estimate_message_tokens(plain)
    assert context.provider_context.messages[0].role == "user"
    assert "x" * 100 in context.provider_context.messages[0].text_content
