from __future__ import annotations

import pytest
from pydantic import ValidationError

from ouro.core.turn_policies import (
    TurnCompletionCandidate,
    TurnPolicyDecision,
    TurnPolicyRegistration,
    TurnPolicyRegistry,
)


def _candidate() -> TurnCompletionCandidate:
    return TurnCompletionCandidate(
        session_id="ses_1",
        run_id="run_1",
        turn_id="turn_1",
        assistant_text="candidate",
    )


def test_turn_policy_decision_requires_feedback_only_when_continuing() -> None:
    assert TurnPolicyDecision().action == "allow"
    with pytest.raises(ValidationError, match="requires feedback"):
        TurnPolicyDecision(action="continue")
    with pytest.raises(ValidationError, match="cannot contain feedback"):
        TurnPolicyDecision(action="allow", feedback="unexpected")


@pytest.mark.asyncio
async def test_turn_policies_merge_continue_feedback_in_stable_order() -> None:
    registry = TurnPolicyRegistry()
    registry.replace_owner(
        "policies",
        (
            TurnPolicyRegistration(
                plugin_id="dev.ouro.second",
                policy_id="continue",
                priority=20,
                handler=lambda _candidate: TurnPolicyDecision(
                    action="continue",
                    feedback="second",
                ),
            ),
            TurnPolicyRegistration(
                plugin_id="dev.ouro.allow",
                policy_id="allow",
                priority=0,
                handler=lambda _candidate: TurnPolicyDecision(),
            ),
            TurnPolicyRegistration(
                plugin_id="dev.ouro.first",
                policy_id="continue",
                priority=10,
                handler=lambda _candidate: TurnPolicyDecision(
                    action="continue",
                    feedback="first",
                ),
            ),
        ),
    )

    result = await registry.evaluate(_candidate())

    assert result.should_continue is True
    assert result.feedback == "first\n\nsecond"
    assert [item.plugin_id for item in result.decisions] == [
        "dev.ouro.first",
        "dev.ouro.second",
    ]


@pytest.mark.asyncio
async def test_turn_policy_failure_blocks_completion_and_redacts_details() -> None:
    registry = TurnPolicyRegistry()

    def fail(_candidate):
        raise RuntimeError("token=turn-secret")

    registry.replace_owner(
        "policy",
        (
            TurnPolicyRegistration(
                plugin_id="dev.ouro.guard",
                policy_id="completion",
                handler=fail,
            ),
        ),
    )

    result = await registry.evaluate(_candidate())

    assert result.should_continue is False
    assert result.failures[0].message == "token=***"
