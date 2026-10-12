"""TUI adapter for the public plan confirmation presentation contract."""

from __future__ import annotations

from ouro.sdk.plugins.interactions import (
    PlanConfirmationRequest,
    parse_plan_confirmation,
)
from ouro.tui.state import PendingInteraction


def project_plan_confirmation(
    interaction: PendingInteraction,
) -> PlanConfirmationRequest | None:
    """Project a typed request; permissions and control ownership remain in the runtime."""

    if interaction.session_id is None:
        return None
    try:
        return parse_plan_confirmation(interaction.request)
    except ValueError:
        return None


__all__ = ["project_plan_confirmation"]
