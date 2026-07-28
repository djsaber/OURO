import pytest
from pydantic import ValidationError

from ouro.core.approvals import ApprovalRecord
from ouro.core.interactions import AnswerResolution, UnattendedResolution
from ouro.core.tools import ToolDefinition


def test_answer_resolution_keeps_opaque_json_and_user_provenance() -> None:
    value = {
        "type": "custom_form",
        "fields": [{"id": "region", "value": ["east", "west"]}],
    }

    resolution = AnswerResolution(value=value)

    assert resolution.kind == "answer"
    assert resolution.value == value
    assert resolution.resolved_by == "user"
    assert resolution.resolver_id is None


def test_host_policy_answer_requires_resolver_identity() -> None:
    with pytest.raises(ValidationError, match="requires resolver_id"):
        AnswerResolution(value=True, resolved_by="host_policy")


def test_user_answer_cannot_claim_policy_resolver_identity() -> None:
    with pytest.raises(ValidationError, match="cannot contain resolver_id"):
        AnswerResolution(value=True, resolver_id="ouro.afk")


def test_unattended_resolution_has_distinct_non_answer_kind() -> None:
    resolution = UnattendedResolution(
        resolver_id="ouro.afk",
        reason="AFK mode does not wait for user input",
    )

    assert resolution.kind == "unattended"
    assert resolution.resolved_by == "host_policy"
    assert not hasattr(resolution, "value")


def test_host_policy_approval_requires_resolver_identity() -> None:
    with pytest.raises(ValidationError, match="requires resolver_id"):
        ApprovalRecord(
            session_id="ses_1",
            run_id="run_1",
            tool_call_id="call_1",
            status="approved",
            resolved_by="host_policy",
        )


def test_tool_must_be_resumable_to_support_unattended_resolution() -> None:
    with pytest.raises(ValidationError, match="requires a resumable tool"):
        ToolDefinition(
            name="Invalid",
            description="Invalid capability declaration.",
            supports_unattended=True,
        )


def test_tool_must_be_resumable_to_bind_a_pending_interaction_to_a_controller() -> None:
    with pytest.raises(ValidationError, match="interaction controller binding requires"):
        ToolDefinition(
            name="Invalid",
            description="Invalid interaction ownership declaration.",
            interaction_controller_id="dev.example/session_controllers/mode",
        )
