from __future__ import annotations

from ouro.application.contracts.models import AvailableModel
from ouro.core.reasoning import (
    REASONING_EFFORTS,
    ReasoningCapability,
    ReasoningRequest,
)
from ouro.tui.menus.labels import APPROVAL_DESCRIPTIONS, THINKING_LABELS
from ouro.tui.menus.models import Choice
from ouro.tui.menus.sorting import sorted_models
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme

REASONING_REQUESTS: dict[str, ReasoningRequest] = {
    "default": ReasoningRequest(),
    "enabled": ReasoningRequest(mode="enabled"),
    "disabled": ReasoningRequest(mode="disabled"),
    **{effort: ReasoningRequest(mode="enabled", effort=effort) for effort in REASONING_EFFORTS},
}
UNAVAILABLE_MODEL_LABEL = "Unavailable model"
SUMMARY_LABELS = {
    "provider_default": "Provider default",
    "auto": "Auto",
    "concise": "Concise",
    "detailed": "Detailed",
    "summarized": "Summarized",
    "omitted": "Omitted",
}


def model_display_label(model: AvailableModel) -> str:
    return model.display_name or model.model_id


def model_selection_label(
    route: str, models: tuple[AvailableModel, ...], *, theme: TuiTheme = DEFAULT_TUI_THEME
) -> str:
    model = next((candidate for candidate in models if candidate.key == route), None)
    if model is None:
        return UNAVAILABLE_MODEL_LABEL
    name = model_display_label(model)
    return (
        theme.inline_separator.join((name, model.provider_display_name))
        if model.provider_display_name
        else name
    )


def approval_choices(
    presets: tuple[str, ...], *, selected: str, theme: TuiTheme
) -> tuple[Choice, ...]:
    return tuple(
        Choice(
            mode,
            mode,
            marker=theme.selected_choice_marker if mode == selected else "",
            trailing=APPROVAL_DESCRIPTIONS[mode],
        )
        for mode in presets
    )


def reasoning_values(capability: ReasoningCapability) -> tuple[str, ...]:
    if capability.activation == "unsupported":
        return ()
    if capability.supported_efforts:
        disabled = ("disabled",) if capability.activation == "toggle" else ()
        return (*disabled, *capability.supported_efforts)
    if capability.activation == "toggle":
        return ("enabled", "disabled")
    return ()


def reasoning_choices(
    capability: ReasoningCapability,
    *,
    selected: str,
    theme: TuiTheme,
    requested: ReasoningRequest | None = None,
) -> tuple[Choice, ...]:
    efforts = tuple(
        Choice(
            value,
            THINKING_LABELS[value],
            marker=theme.selected_choice_marker if value == selected else "",
            trailing="current" if value == selected else "",
            group="EFFORT" if capability.supported_summaries else "",
        )
        for value in reasoning_values(capability)
    )
    if not capability.supported_summaries:
        return efforts
    summary = requested.summary if requested is not None else None
    summaries = tuple(
        Choice(
            f"summary:{value}",
            "Endpoint default" if value == "default" else SUMMARY_LABELS[value],
            marker=theme.selected_choice_marker if value == (summary or "default") else "",
            group="SUMMARY",
        )
        for value in ("default", "provider_default", *capability.supported_summaries)
    )
    return (*efforts, *summaries)


def reasoning_request(value: str, current: ReasoningRequest) -> ReasoningRequest:
    """Change one control while preserving the other requested control."""
    if value.startswith("summary:"):
        summary = value.removeprefix("summary:")
        return ReasoningRequest.model_validate(
            {**current.model_dump(), "summary": None if summary == "default" else summary}
        )
    return REASONING_REQUESTS[value].model_copy(update={"summary": current.summary})


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
            model_display_label(model),
            trailing=(
                model.model_id
                if model.display_name and model.display_name.casefold() != model.model_id.casefold()
                else ""
            ),
            marker=theme.selected_choice_marker if model.key == selected else "",
            group=model.provider_display_name or "Other models",
        )
        for model in sorted_models(models)
    )
