from __future__ import annotations

import pytest

from ouro.core.context_providers import AttributedContextFragment, ContextFragment
from ouro.core.continuations import ContinuationDecision
from ouro.core.session_controls import HostSessionInput, SessionInputRequest
from ouro.core.state import RunContinuationSettlementPlan
from ouro.sdk.plugins import v5 as sdk


@pytest.mark.parametrize(
    "factory, fields",
    [
        (SessionInputRequest, {"prompt": "Continue"}),
        (HostSessionInput, {"prompt": "Continue", "host_source_id": "test"}),
        (ContextFragment, {"text": "Context"}),
        (
            AttributedContextFragment,
            {"text": "Context", "plugin_id": "test", "provider_id": "context", "fragment_index": 0},
        ),
        (ContinuationDecision, {}),
        (
            RunContinuationSettlementPlan,
            {
                "plugin_id": "test",
                "policy_id": "continue",
                "execution_contract_hash": "test",
                "contract_hash": "test",
                "action": "pause",
            },
        ),
        (sdk.SessionInput, {"prompt": "Continue"}),
        (sdk.ContextFragment, {"text": "Context"}),
        (sdk.ContinuationDecision, {}),
    ],
)
def test_scaffold_defaults_to_user_and_preserves_explicit_role(factory, fields):
    assert factory(**fields).role == "user"
    assert factory(**fields, role="system").role == "system"
