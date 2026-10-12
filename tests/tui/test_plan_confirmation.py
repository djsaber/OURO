from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest

from ouro.sdk.plugins.interactions import PlanConfirmationRequest
from ouro.tui.plan_confirmation import project_plan_confirmation
from ouro.tui.state import PendingInteraction


def _interaction(*, plugin_class: Literal["builtin", "user"] = "builtin") -> PendingInteraction:
    request = PlanConfirmationRequest(
        plan_path="C:/private/session/plan.md",
        plan="# Plan\n\n1. Inspect.\n2. Implement.\n3. Verify.",
        plan_digest="digest",
    )
    return PendingInteraction(
        kind="question",
        session_id="ses_1",
        run_id="run_1",
        tool_call_id="call_exit",
        tool_name="ExitPlanMode" if plugin_class == "builtin" else "UserConfirmPlan",
        request=request.to_json(),
        canonical_tool_id=f"dev.example.{plugin_class}/tools/confirm",
        plugin_id=f"dev.example.{plugin_class}",
        plugin_class=plugin_class,
    )


@pytest.mark.parametrize("plugin_class", ["builtin", "user"])
def test_project_plan_confirmation_uses_public_type_without_product_identity(plugin_class) -> None:
    projected = project_plan_confirmation(_interaction(plugin_class=plugin_class))

    assert projected is not None
    assert projected.plan == "# Plan\n\n1. Inspect.\n2. Implement.\n3. Verify."


@pytest.mark.parametrize("payload", [{"type": "plan_confirmation"}, {"type": "unknown"}, "text"])
def test_invalid_or_unrecognized_confirmation_uses_generic_input(payload) -> None:
    interaction = replace(_interaction(), request=payload)
    assert project_plan_confirmation(interaction) is None
