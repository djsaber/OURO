from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from ouro.application.agent_models import AgentInteractionView
from ouro.application.runtime_activity import reduce_runtime_activity
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, RunStatus
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme

InteractionKind = Literal["approval", "question"]


def canonical_event_order(events: Iterable[EventRecord]) -> tuple[EventRecord, ...]:
    """Return canonical event order without sorting an already ordered snapshot."""

    materialized = tuple(events)
    previous_key: tuple[int, str] | None = None
    for event in materialized:
        key = (event.sequence, event.event_id)
        if previous_key is not None and key < previous_key:
            return tuple(sorted(materialized, key=lambda item: (item.sequence, item.event_id)))
        previous_key = key
    return materialized


def format_compact_count(value: int) -> str:
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= divisor:
            scaled = value / divisor
            precision = 0 if scaled >= 100 else 1
            return f"{scaled:.{precision}f}{suffix}"
    return str(value)


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining = divmod(seconds, 60)
    return f"{int(minutes)}m {remaining:04.1f}s"


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
    state_mark = (
        theme.expanded_disclosure_marker
        if expanded
        else theme.collapsed_disclosure_marker
    )
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


def project_run_presentation(
    run: RunRecord,
    messages: Iterable[MessageRecord] = (),
) -> RunPresentation:
    requests = tuple(
        ModelRequestPresentation(
            message_id=message.message_id,
            model=response.endpoint_id or response.model_id,
            reasoning_seconds=(
                max(0.0, (response.reasoning_ended_at - response.started_at).total_seconds())
                if response.reasoning_ended_at is not None
                else None
            ),
        )
        for message in messages
        if message.run_id == run.run_id and (response := message.model_response) is not None
    )
    model = requests[-1].model if requests else run.model or "unknown model"
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
) -> tuple[RunPresentation, ...]:
    messages_by_run: dict[str, list[MessageRecord]] = {}
    for message in messages:
        if message.run_id is not None:
            messages_by_run.setdefault(message.run_id, []).append(message)
    return tuple(project_run_presentation(run, messages_by_run.get(run.run_id, ())) for run in runs)


@dataclass
class _TextAccumulator:
    """Accumulate streamed text without copying the full prefix for every delta."""

    _parts: list[str] = field(default_factory=list)
    _value: str | None = ""

    @property
    def value(self) -> str:
        if self._value is None:
            self._value = "".join(self._parts)
        return self._value

    def append(self, text: str) -> None:
        if text:
            self._parts.append(text)
            self._value = None

    def replace(self, text: str) -> None:
        self._parts = [text] if text else []
        self._value = text


@dataclass(frozen=True)
class PendingInteraction:
    kind: InteractionKind
    run_id: str
    tool_call_id: str
    tool_name: str
    request_id: str | None = None
    request: object | None = None
    canonical_tool_id: str | None = None
    plugin_id: str | None = None
    plugin_class: Literal["builtin", "user"] | None = None
    session_id: str | None = None
    delegation_id: str | None = None
    agent_name: str | None = None

    @property
    def is_subagent(self) -> bool:
        return self.delegation_id is not None


@dataclass(frozen=True)
class ProjectionUpdate:
    event: EventRecord
    activity: RuntimeActivitySnapshot
    assistant_delta: str = ""
    reasoning_delta: str = ""
    reasoning_started_at: datetime | None = None
    reasoning_duration_seconds: float | None = None
    pending_interaction: PendingInteraction | None = None
    interaction_changed: bool = False
    diagnostic: str | None = None


@dataclass
class TuiSessionProjection:
    """Small UI projection derived exclusively from the canonical event stream."""

    session_id: str
    activity: RuntimeActivitySnapshot = field(init=False)
    _assistant_text: _TextAccumulator = field(default_factory=_TextAccumulator, init=False)
    _reasoning_text: _TextAccumulator = field(default_factory=_TextAccumulator, init=False)
    _pending_interactions: dict[str, PendingInteraction] = field(
        default_factory=dict,
        init=False,
    )
    _last_sequence: int = field(default=-1, init=False)
    _run_id: str | None = field(default=None, init=False)
    _model_request_started_at: datetime | None = field(default=None, init=False)
    _reasoning_seen: bool = field(default=False, init=False)
    _reasoning_finished: bool = field(default=False, init=False)
    _reasoning_duration_seconds: float | None = field(default=None, init=False)
    _model: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.activity = RuntimeActivitySnapshot(
            session_id=self.session_id,
            status="idle",
            activity="idle",
        )

    @property
    def pending_interactions(self) -> tuple[PendingInteraction, ...]:
        return tuple(self._pending_interactions.values())

    @property
    def pending_interaction(self) -> PendingInteraction | None:
        return next(iter(self._pending_interactions.values()), None)

    @property
    def assistant_text(self) -> str:
        return self._assistant_text.value

    @assistant_text.setter
    def assistant_text(self, text: str) -> None:
        self._assistant_text.replace(text)

    @property
    def reasoning_text(self) -> str:
        return self._reasoning_text.value

    @reasoning_text.setter
    def reasoning_text(self, text: str) -> None:
        self._reasoning_text.replace(text)

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def reasoning_started_at(self) -> datetime | None:
        return self._model_request_started_at if self._reasoning_seen else None

    @property
    def reasoning_duration_seconds(self) -> float | None:
        return self._reasoning_duration_seconds

    @property
    def last_sequence(self) -> int:
        return max(0, self._last_sequence)

    def restore(
        self,
        events: tuple[EventRecord, ...],
        *,
        event_high_water: int | None = None,
    ) -> None:
        for event in canonical_event_order(events):
            self.apply(event)
        if event_high_water is not None:
            self._last_sequence = max(self._last_sequence, event_high_water)

    def apply(self, event: EventRecord) -> ProjectionUpdate | None:
        if event.session_id != self.session_id or event.sequence <= self._last_sequence:
            return None
        self._last_sequence = event.sequence
        self.activity = reduce_runtime_activity(self.activity, event)

        payload = event.payload
        assistant_delta = ""
        reasoning_delta = ""
        reasoning_started_at = None
        reasoning_duration_seconds = None
        interaction_changed = False
        diagnostic = None

        if event.event_type == "run_started" and event.run_id != self._run_id:
            self._run_id = event.run_id
            self._assistant_text.replace("")
            self._reasoning_text.replace("")
            self._pending_interactions.clear()
            self._model_request_started_at = None
            self._reasoning_seen = False
            self._reasoning_finished = False
            self._reasoning_duration_seconds = None
            self._model = None
        elif event.event_type == "model_request_started":
            self._model_request_started_at = event.created_at
            self._reasoning_seen = False
            self._reasoning_finished = False
            self._reasoning_duration_seconds = None
            self._model = _string(payload.get("endpoint")) or _string(payload.get("model"))
        elif event.event_type == "model_text_delta":
            assistant_delta = _string(payload.get("text")) or ""
            self._assistant_text.append(assistant_delta)
            reasoning_duration_seconds = self._finish_reasoning(event.created_at)
        elif event.event_type == "model_reasoning_delta":
            reasoning_delta = _string(payload.get("reasoning_content")) or ""
            self._reasoning_text.append(reasoning_delta)
            if reasoning_delta and not self._reasoning_seen:
                self._reasoning_seen = True
                reasoning_started_at = self._model_request_started_at or event.created_at
        elif event.event_type == "model_tool_call_delta":
            reasoning_duration_seconds = self._finish_reasoning(event.created_at)
        elif event.event_type == "model_message_completed":
            reasoning_duration_seconds = self._finish_reasoning(event.created_at)
        elif event.event_type == "approval_requested" and event.run_id is not None:
            tool_call_id = _string(payload.get("tool_call_id"))
            approval_id = _string(payload.get("approval_id"))
            if tool_call_id is not None and approval_id is not None:
                self._pending_interactions[approval_id] = PendingInteraction(
                    kind="approval",
                    run_id=event.run_id,
                    tool_call_id=tool_call_id,
                    tool_name=_string(payload.get("tool_name")) or "tool",
                    request_id=approval_id,
                    session_id=event.session_id,
                )
                interaction_changed = True
        elif event.event_type == "ask_user_requested" and event.run_id is not None:
            tool_call_id = _string(payload.get("tool_call_id"))
            if tool_call_id is not None:
                self._pending_interactions[f"question:{event.run_id}:{tool_call_id}"] = (
                    PendingInteraction(
                        kind="question",
                        run_id=event.run_id,
                        tool_call_id=tool_call_id,
                        tool_name=_string(payload.get("tool_name")) or "tool",
                        request=payload.get("request"),
                        canonical_tool_id=_string(payload.get("canonical_tool_id")),
                        plugin_id=_string(payload.get("plugin_id")),
                        plugin_class=_plugin_class(payload.get("plugin_class")),
                        session_id=event.session_id,
                    )
                )
                interaction_changed = True
        elif event.event_type in {"approval_resolved", "ask_user_resolved"}:
            approval_id = _string(payload.get("approval_id"))
            tool_call_id = _string(payload.get("tool_call_id"))
            removed = tuple(
                key
                for key, interaction in self._pending_interactions.items()
                if (approval_id is not None and interaction.request_id == approval_id)
                or (approval_id is None and interaction.tool_call_id == tool_call_id)
            )
            for key in removed:
                del self._pending_interactions[key]
            if removed:
                interaction_changed = True
        elif event.event_type in {
            "run_abort_requested",
            "run_completed",
            "run_aborted",
            "run_failed",
        }:
            reasoning_duration_seconds = self._finish_reasoning(event.created_at)
            if self._pending_interactions:
                self._pending_interactions.clear()
                interaction_changed = True

        if event.event_type in {"diagnostic", "persistence_warning"}:
            diagnostic = _string(payload.get("message")) or _string(payload.get("error"))
        elif event.event_type == "event_sink_failed":
            message = _string(payload.get("error_message")) or "unknown delivery error"
            diagnostic = f"Live event delivery failed: {message}"
        elif event.event_type == "run_failed":
            diagnostic = _string(payload.get("error")) or "Run failed"

        return ProjectionUpdate(
            event=event,
            activity=self.activity,
            assistant_delta=assistant_delta,
            reasoning_delta=reasoning_delta,
            reasoning_started_at=reasoning_started_at,
            reasoning_duration_seconds=reasoning_duration_seconds,
            pending_interaction=self.pending_interaction,
            interaction_changed=interaction_changed,
            diagnostic=diagnostic,
        )

    def _finish_reasoning(self, ended_at: datetime) -> float | None:
        if (
            not self._reasoning_seen
            or self._reasoning_finished
            or self._model_request_started_at is None
        ):
            return None
        self._reasoning_finished = True
        self._reasoning_duration_seconds = max(
            0.0,
            (ended_at - self._model_request_started_at).total_seconds(),
        )
        return self._reasoning_duration_seconds


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


def _plugin_class(value: object) -> Literal["builtin", "user"] | None:
    return value if value in {"builtin", "user"} else None
