from __future__ import annotations

from ouro.application.models import AvailableModel
from ouro.tui.screens import Choice
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme


def model_choices(
    models: tuple[AvailableModel, ...],
    *,
    selected: str | None = None,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    """Project model routes through the shared provider-grouped menu contract."""

    return tuple(
        Choice(
            model.key,
            model.display_name or model.model_id,
            trailing=model.endpoint_id,
            marker=theme.selected_choice_marker if model.key == selected else "",
            group=model.provider_id,
        )
        for model in models
    )
