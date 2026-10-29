from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from ouro.application.contracts.execution import RuntimeActivitySnapshot
from ouro.application.contracts.sessions import SessionObservationFacts, SessionObservationSnapshot
from ouro.application.contracts.tool_activity import PendingInteraction
from ouro.application.observations.activity import (
    apply_observed_activity,
    overlay_request_activity,
    overlay_run_activity,
    reduce_runtime_activity,
)
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import ACTIVE_RUN_STATUSES


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
    """Current request output and interaction state, without a second Run transcript."""

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
    _request_id: str | None = field(default=None, init=False)
    _draft_cursor: int = field(default=0, init=False)
    _request_closed: bool = field(default=False, init=False)
    _request_text_length: int = field(default=0, init=False)
    _request_reasoning_length: int = field(default=0, init=False)
    _request_turn_id: str | None = field(default=None, init=False)
    _message_sequence: int = field(default=0, init=False)
    _live_messages: dict[str, int] = field(default_factory=dict, init=False)

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

    def has_message(self, message_id: str, sequence: int) -> bool:
        return sequence <= self._message_sequence or message_id in self._live_messages

    def remember_message(self, message_id: str, sequence: int) -> None:
        self._live_messages[message_id] = sequence

    def confirm_messages(self, sequence: int) -> None:
        self._message_sequence = sequence
        self._live_messages = {
            identity: position
            for identity, position in self._live_messages.items()
            if position > sequence
        }

    def apply_response(self, message: MessageRecord) -> tuple[str, str, float | None]:
        context_id = (
            self._request_id if message.turn_id == self._request_turn_id else message.message_id
        )
        self._run_id = message.run_id
        self._request_turn_id = message.turn_id
        self.remember_message(message.message_id, message.sequence)
        return self._complete_response(
            context_id,
            message.text_content,
            message.reasoning_content,
            message.created_at,
        )

    def is_current_response(self, message: MessageRecord) -> bool:
        return message.turn_id is not None and message.turn_id == self._request_turn_id

    def apply_facts(self, facts: SessionObservationFacts) -> bool:
        self.activity = apply_observed_activity(self.activity, facts)
        changed = {(identity.run_id, identity.tool_call_id) for identity in facts.changed_calls}
        self._pending_interactions = {
            key: value
            for key, value in self._pending_interactions.items()
            if (value.run_id, value.tool_call_id) not in changed
            and not any(
                run.run_id == value.run_id
                and (run.abort_requested or run.status not in ACTIVE_RUN_STATUSES)
                for run in facts.runs
            )
        }
        for interaction in facts.pending_interactions:
            key = (
                interaction.request_id
                or f"question:{interaction.run_id}:{interaction.tool_call_id}"
            )
            self._pending_interactions[key] = interaction
        request = facts.current_request
        changed_request = bool(
            request
            and request.response_closed_at is None
            and request.context_id != self._request_id
        )
        if request is not None and changed_request:
            self._start_request(request.context_id)
            self._request_turn_id = request.turn_id
            self._model_request_started_at = request.started_at
            self._model = request.model_id
        return changed_request

    def restore(
        self,
        events: tuple[EventRecord, ...],
    ) -> None:
        for event in events:
            self.apply(event)

    def apply(self, event: EventRecord) -> ProjectionUpdate | None:
        if event.session_id != self.session_id:
            return None
        if event.history_epoch != self.history_epoch:
            return None
        if event.stream is not None:
            position = event.stream
            if self._request_id != position.context_id:
                if self._request_id is not None and event.sequence < self._last_sequence:
                    return None
                self._start_request(position.context_id)
                self._request_turn_id = event.turn_id
            if self._request_closed:
                return None
            if position.cursor is not None:
                if position.cursor <= self._draft_cursor:
                    return None
                self._draft_cursor = position.cursor
        elif event.sequence <= self._last_sequence:
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
            self._start_request(_string(payload.get("context_id")))
            self._request_turn_id = event.turn_id or _string(payload.get("turn_id"))
            self._model_request_started_at = event.created_at
            self._reasoning_seen = False
            self._reasoning_finished = False
            self._reasoning_duration_seconds = None
            self._model = _string(payload.get("model"))
        elif event.event_type == "model_text_delta":
            assistant_delta = _string(payload.get("text")) or ""
            self._assistant_text.append(assistant_delta)
            self._request_text_length += len(assistant_delta)
            reasoning_duration_seconds = self._finish_reasoning(event.created_at)
        elif event.event_type == "model_reasoning_delta":
            reasoning_delta = _string(payload.get("reasoning_content")) or ""
            self._reasoning_text.append(reasoning_delta)
            self._request_reasoning_length += len(reasoning_delta)
            if reasoning_delta and not self._reasoning_seen:
                self._reasoning_seen = True
                reasoning_started_at = self._model_request_started_at or event.created_at
        elif event.event_type == "model_tool_call_delta":
            reasoning_duration_seconds = self._finish_reasoning(event.created_at)
        elif event.event_type == "model_message_completed":
            reasoning_duration_seconds = self._finish_reasoning(event.created_at)
        elif event.event_type == "model_response_committed":
            context_id = _string(payload.get("context_id"))
            identity, sequence = payload.get("message_id"), payload.get("message_sequence")
            if isinstance(identity, str) and isinstance(sequence, int):
                if self.has_message(identity, sequence):
                    return None
                self.remember_message(identity, sequence)
            assistant_delta, reasoning_delta, reasoning_duration_seconds = self._complete_response(
                context_id,
                _string(payload.get("text")) or "",
                _string(payload.get("reasoning_content")) or "",
                event.created_at,
            )
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

        self._last_sequence = max(self._last_sequence, event.sequence)
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

    def _complete_response(
        self,
        context_id: str | None,
        text: str,
        reasoning: str,
        ended_at: datetime,
    ) -> tuple[str, str, float | None]:
        if context_id != self._request_id:
            self._start_request(context_id)
        answer = text[self._request_text_length :]
        thought = reasoning[self._request_reasoning_length :]
        self._assistant_text.replace(text)
        self._reasoning_text.replace(reasoning)
        self._request_text_length = len(text)
        self._request_reasoning_length = len(reasoning)
        self._request_closed = True
        return answer, thought, self._finish_reasoning(ended_at)

    def _start_request(self, context_id: str | None) -> None:
        self._assistant_text.replace("")
        self._reasoning_text.replace("")
        self._reasoning_seen = False
        self._reasoning_finished = False
        self._reasoning_duration_seconds = None
        self._request_id = context_id
        self._draft_cursor = 0
        self._request_closed = False
        self._request_text_length = 0
        self._request_reasoning_length = 0

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


def restore_session_content(snapshot: SessionObservationSnapshot) -> SessionContentProjection:
    """Rebuild content without recovering execution or depending on a Host renderer."""
    projection = SessionContentProjection(
        snapshot.session.session_id, history_epoch=snapshot.session.history_epoch
    )
    projection.confirm_messages(snapshot.session.message_count)
    recent_id = snapshot.session.active_run_id or snapshot.session.last_run_id
    recent = next(
        (
            run
            for run in snapshot.runs
            if run.run_id == recent_id and run.history_epoch == snapshot.session.history_epoch
        ),
        None,
    )
    if recent is not None:
        projection._run_id = recent.run_id
        projection._model = recent.model
        projection.activity = overlay_run_activity(
            projection.activity, recent, tool_calls=snapshot.active_tool_calls
        )
    header = snapshot.current_request
    if header is not None and recent is not None and header.run_id == recent.run_id:
        projection._model_request_started_at = header.started_at
        projection.activity = overlay_request_activity(projection.activity, recent, header)
    projection._pending_interactions = {
        interaction.request_id or f"question:{interaction.run_id}:{interaction.tool_call_id}": (
            interaction
        )
        for interaction in snapshot.pending_interactions
    }
    projection.restore(snapshot.events)
    if recent is not None:
        projection.activity = overlay_run_activity(
            projection.activity, recent, tool_calls=snapshot.active_tool_calls
        )
    projection.activity = projection.activity.model_copy(
        update={
            "sequence": projection.last_sequence,
            "context_status": snapshot.context_status,
            "effective_context_metrics": snapshot.effective_context_metrics,
        }
    )
    return projection


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _plugin_class(value: object) -> Literal["builtin", "user"] | None:
    return value if value in {"builtin", "user"} else None
