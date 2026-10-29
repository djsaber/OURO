from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ouro.application.contracts.agents import AgentInteractionView
from ouro.application.contracts.models import AvailableModel
from ouro.application.contracts.tool_activity import InteractionKind as InteractionKind
from ouro.application.contracts.tool_activity import PendingInteraction as PendingInteraction
from ouro.application.observations.content import ProjectionUpdate as ProjectionUpdate
from ouro.application.observations.content import SessionContentProjection
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, RunStatus
from ouro.tui.features.providers.model_menus import model_display_label
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme

TuiSessionProjection = SessionContentProjection


def format_compact_count(value: int) -> str:
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            scaled = value / divisor
            precision = 0 if scaled >= 100 else 1
            return f"{scaled:.{precision}f}{suffix}"
    return str(value)


def format_duration(seconds: float) -> str:
    """Format elapsed time with day/hour/minute carries at display precision."""
    seconds = max(0.0, seconds)
    if seconds < 1:
        milliseconds = round(seconds * 1000)
        if milliseconds < 1000:
            return f"{milliseconds}ms"

    # Round before splitting units so the displayed remainder cannot reach 60s.
    minutes, tenths = divmod(round(seconds * 10), 600)
    if minutes == 0:
        return f"{tenths / 10:.1f}s"
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.extend((f"{minutes}m", f"{tenths / 10:04.1f}s"))
    return " ".join(parts)


def format_reasoning_label(
    label: str,
    duration_seconds: float | None,
    *,
    expanded: bool,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> str:
    text, duration = format_reasoning_label_parts(
        label,
        duration_seconds,
        expanded=expanded,
        theme=theme,
    )
    if duration is not None:
        text += f"{theme.inline_separator}{duration}"
    return text


def format_reasoning_label_parts(
    label: str,
    duration_seconds: float | None,
    *,
    expanded: bool,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[str, str | None]:
    state_mark = theme.expanded_disclosure_marker if expanded else theme.collapsed_disclosure_marker
    duration = format_duration(duration_seconds) if duration_seconds is not None else None
    return f"{state_mark} {label}", duration


@dataclass(frozen=True)
class ModelRequestPresentation:
    message_id: str
    model: str
    reasoning_seconds: float | None = None


@dataclass(frozen=True)
class RunPresentation:
    run_id: str
    status: RunStatus
    model: str
    duration_seconds: float | None
    error: str | None = None
    requests: tuple[ModelRequestPresentation, ...] = ()


def describe_run_pause(run: RunRecord) -> str:
    pause = run.metadata.get("pause")
    reason = pause.get("reason") if isinstance(pause, dict) else None
    descriptions = {
        "context_blocked": (
            "context preparation is blocked; check its diagnostics, "
            "compact the session or change the model"
        ),
        "turn_policy_blocked": (
            "the turn policy is blocked; resolve its reported failure before retrying"
        ),
        "model_rounds": "the model-round budget has been reached",
        "input_tokens": "the input-token budget has been reached",
        "output_tokens": "the output-token budget has been reached",
        "elapsed_time": "the elapsed-time budget has been reached",
    }
    detail = (
        descriptions.get(reason, "execution conditions are still blocked")
        if isinstance(reason, str)
        else "execution conditions are still blocked"
    )
    return f"Run remains paused: {detail}."


def project_run_presentation(
    run: RunRecord,
    messages: Iterable[MessageRecord] = (),
    *,
    models: Iterable[AvailableModel] = (),
) -> RunPresentation:
    available_models = tuple(models)
    requests = tuple(
        ModelRequestPresentation(
            message_id=message.message_id,
            model=_presented_model_name(
                available_models,
                provider_id=response.provider_id,
                endpoint_id=response.endpoint_id,
                model_id=response.model_id,
            ),
            reasoning_seconds=(
                max(0.0, (response.reasoning_ended_at - response.started_at).total_seconds())
                if response.reasoning_ended_at is not None
                else None
            ),
        )
        for message in messages
        if message.run_id == run.run_id and (response := message.model_response) is not None
    )
    model = (
        requests[-1].model
        if requests
        else _presented_model_name(
            available_models,
            endpoint_id=run.model_endpoint_id,
            model_id=run.model,
        )
    )
    duration = (
        max(0.0, (run.ended_at - run.started_at).total_seconds())
        if run.ended_at is not None
        else None
    )
    return RunPresentation(
        run_id=run.run_id,
        status=run.status,
        model=model,
        duration_seconds=duration,
        error=run.error,
        requests=requests,
    )


def project_run_presentations(
    runs: Iterable[RunRecord],
    messages: Iterable[MessageRecord] = (),
    *,
    models: Iterable[AvailableModel] = (),
) -> tuple[RunPresentation, ...]:
    available_models = tuple(models)
    messages_by_run: dict[str, list[MessageRecord]] = {}
    for message in messages:
        if message.run_id is not None:
            messages_by_run.setdefault(message.run_id, []).append(message)
    return tuple(
        project_run_presentation(
            run,
            messages_by_run.get(run.run_id, ()),
            models=available_models,
        )
        for run in runs
    )


def _presented_model_name(
    models: tuple[AvailableModel, ...],
    *,
    provider_id: str | None = None,
    endpoint_id: str | None = None,
    model_id: str | None = None,
) -> str:
    candidates = models
    if provider_id is not None:
        candidates = tuple(model for model in candidates if model.provider_id == provider_id)
    if endpoint_id is not None:
        candidates = tuple(model for model in candidates if model.endpoint_id == endpoint_id)
    elif model_id is not None:
        candidates = tuple(model for model in candidates if model.model_id == model_id)
    if len(candidates) > 1 and model_id is not None:
        candidates = tuple(model for model in candidates if model.model_id == model_id)
    if len(candidates) == 1:
        model = candidates[0]
        return model_display_label(model)
    return model_id or "unknown model"


def question_text(interaction: PendingInteraction) -> str:
    request = interaction.request
    if isinstance(request, dict):
        for key in ("question", "prompt", "message", "title"):
            value = request.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(request, str) and request.strip():
        return request.strip()
    return f"{interaction.tool_name} is waiting for your input"


def project_agent_interaction(interaction: AgentInteractionView) -> PendingInteraction:
    """Adapt the canonical agent-tree interaction DTO to the TUI interaction model."""

    return PendingInteraction(
        kind="approval" if interaction.kind == "approval" else "question",
        run_id=interaction.child_run_id,
        tool_call_id=interaction.tool_call_id,
        tool_name=interaction.tool_name,
        request_id=interaction.request_id,
        request=interaction.input_request,
        canonical_tool_id=interaction.canonical_tool_id,
        plugin_id=interaction.plugin_id,
        plugin_class=interaction.plugin_class,
        session_id=interaction.child_session_id,
        delegation_id=interaction.delegation_id,
        agent_name=interaction.agent_name,
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None
