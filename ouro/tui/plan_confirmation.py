"""TUI adapter for the builtin Plan mode confirmation protocol."""

from __future__ import annotations

from ouro.builtin_plugins.plan_mode.interaction import (
    PlanConfirmationRequest,
    parse_builtin_plan_confirmation,
)
from ouro.core.tools import ToolInvocation
from ouro.tui.state import PendingInteraction


def project_plan_confirmation(
    interaction: PendingInteraction,
) -> PlanConfirmationRequest | None:
    """Recognize an official ExitPlanMode request without trusting its display name."""

    if interaction.session_id is None:
        return None
    invocation = ToolInvocation(
        session_id=interaction.session_id,
        run_id=interaction.run_id,
        tool_call_id=interaction.tool_call_id,
        tool_name=interaction.tool_name,
        canonical_tool_id=interaction.canonical_tool_id,
        plugin_id=interaction.plugin_id,
        plugin_class=interaction.plugin_class,
    )
    try:
        return parse_builtin_plan_confirmation(invocation, interaction.request)
    except ValueError:
        return None


__all__ = ["project_plan_confirmation"]
