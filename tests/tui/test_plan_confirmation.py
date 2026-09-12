from __future__ import annotations

from typing import Literal

from ouro.builtin_plugins.plan.interaction import PlanConfirmationRequest
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
        tool_name="ExitPlanMode",
        request=request.to_json(),
        canonical_tool_id="dev.ouro.plan/tools/exit",
        plugin_id="dev.ouro.plan",
        plugin_class=plugin_class,
    )


def test_project_plan_confirmation_requires_builtin_tool_provenance() -> None:
    projected = project_plan_confirmation(_interaction())

    assert projected is not None
    assert projected.plan == "# Plan\n\n1. Inspect.\n2. Implement.\n3. Verify."
    assert project_plan_confirmation(_interaction(plugin_class="user")) is None
