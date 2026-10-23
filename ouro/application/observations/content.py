from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from ouro.application.contracts.execution import RuntimeActivitySnapshot
from ouro.application.contracts.sessions import SessionObservationSnapshot
from ouro.application.observations.activity import overlay_run_activity, reduce_runtime_activity
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord

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
class SessionContentProjection:
    """Session content and interaction state derived from canonical events."""

    session_id: str
    history_epoch: int = 0
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
            history_epoch=self.history_epoch,
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
        if event.history_epoch != self.history_epoch:
            return None
        self.activity = reduce_runtime_activity(self.activity, event)

        payload = event.payload
        assistant_delta = ""
        reasoning_delta = ""
        reasoning_started_at = None
        reasoning_duration_seconds = None
        interaction_changed = False
        diagnostic = None

        if (event.event_type == "run_started" and event.run_id != self._run_id) or (
            event.event_type == "run_status_changed" and payload.get("resumed") is True
        ):
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
            self._model = _string(payload.get("model"))
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

        self._last_sequence = event.sequence
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


def active_stream_tail(
    events: Sequence[EventRecord],
    messages: Sequence[MessageRecord] = (),
) -> tuple[str, str]:
    """Return stream text not yet represented by a canonical assistant message.

    ``model_message_completed`` closes the provider stream, but the Runtime
    persists the resulting MessageRecord at a later durable boundary. A Host
    snapshot can legitimately land between those operations, so only an actual
    canonical assistant message proves that the corresponding deltas are
    already present in history.
    """

    ordered = canonical_event_order(events)
    run_id = next((event.run_id for event in ordered if event.run_id is not None), None)
    covered_turns = {
        message.turn_id
        for message in messages
        if message.role == "assistant" and message.run_id == run_id and message.turn_id is not None
    }
    start = 0
    for index, event in enumerate(ordered):
        if event.event_type == "run_status_changed" and event.payload.get("resumed") is True:
            start = index + 1
            continue
        if event.event_type != "model_message_completed":
            continue
        if event.turn_id is not None and event.turn_id in covered_turns:
            start = index + 1
    reasoning: list[str] = []
    answer: list[str] = []
    for event in ordered[start:]:
        value = (
            event.payload.get("reasoning_content")
            if event.event_type == "model_reasoning_delta"
            else event.payload.get("text")
            if event.event_type == "model_text_delta"
            else None
        )
        if not isinstance(value, str):
            continue
        (reasoning if event.event_type == "model_reasoning_delta" else answer).append(value)
    return "".join(reasoning), "".join(answer)


def restore_session_content(snapshot: SessionObservationSnapshot) -> SessionContentProjection:
    """Rebuild content without recovering execution or depending on a Host renderer."""
    projection = SessionContentProjection(
        snapshot.session.session_id, history_epoch=snapshot.session.history_epoch
    )
    projection.restore(snapshot.events, event_high_water=snapshot.event_high_water)
    recent_id = snapshot.session.active_run_id or snapshot.session.last_run_id
    recent = next((run for run in snapshot.runs if run.run_id == recent_id), None)
    if recent is not None:
        projection.activity = overlay_run_activity(projection.activity, recent)
    projection.activity = projection.activity.model_copy(
        update={
            "context_status": snapshot.context_status,
            "effective_context_metrics": snapshot.effective_context_metrics,
        }
    )
    return projection


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _plugin_class(value: object) -> Literal["builtin", "user"] | None:
    return value if value in {"builtin", "user"} else None
