from __future__ import annotations

from ouro.application.models import AvailableModel
from ouro.tui.screens import Choice
from ouro.tui.theme import TUI_DESIGN


def model_choices(
    models: tuple[AvailableModel, ...],
    *,
    selected: str | None = None,
) -> tuple[Choice, ...]:
    """Project model routes through the shared provider-grouped menu contract."""

    return tuple(
        Choice(
            model.key,
            model.display_name or model.model_id,
            trailing=model.endpoint_id,
            marker=TUI_DESIGN.selected_choice_marker if model.key == selected else "",
            group=model.provider_id,
        )
        for model in models
    )
