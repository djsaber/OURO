from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ouro.application.agent_models import AgentInteractionView
from ouro.application.runtime_activity import queued_runtime_activity, reduce_runtime_activity
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.events import EventRecord

InteractionKind = Literal["approval", "question"]


@dataclass(frozen=True)
class PendingInteraction:
    kind: InteractionKind
    run_id: str
    tool_call_id: str
    tool_name: str
    request_id: str | None = None
    request: object | None = None
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
    round_break: bool = False
    pending_interaction: PendingInteraction | None = None
    interaction_changed: bool = False
    diagnostic: str | None = None


@dataclass
class TuiSessionProjection:
    """Small UI projection derived exclusively from the canonical event stream."""

    session_id: str
    activity: RuntimeActivitySnapshot = field(init=False)
    assistant_text: str = ""
    reasoning_text: str = ""
    _pending_interactions: dict[str, PendingInteraction] = field(
        default_factory=dict,
        init=False,
    )
    _last_sequence: int = field(default=-1, init=False)
    _run_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.activity = queued_runtime_activity(self.session_id)

    @property
    def pending_interactions(self) -> tuple[PendingInteraction, ...]:
        return tuple(self._pending_interactions.values())

    @property
    def pending_interaction(self) -> PendingInteraction | None:
        return next(iter(self._pending_interactions.values()), None)

    def restore(self, events: tuple[EventRecord, ...]) -> None:
        for event in sorted(events, key=lambda item: (item.sequence, item.event_id)):
            self.apply(event)

    def apply(self, event: EventRecord) -> ProjectionUpdate | None:
        if event.session_id != self.session_id or event.sequence <= self._last_sequence:
            return None
        self._last_sequence = event.sequence
        self.activity = reduce_runtime_activity(self.activity, event)

        payload = event.payload
        assistant_delta = ""
        reasoning_delta = ""
        round_break = False
        interaction_changed = False
        diagnostic = None

        if event.event_type == "run_started" and event.run_id != self._run_id:
            self._run_id = event.run_id
            self.assistant_text = ""
            self.reasoning_text = ""
            self._pending_interactions.clear()
        elif event.event_type == "model_text_delta":
            assistant_delta = _string(payload.get("text")) or ""
            self.assistant_text += assistant_delta
        elif event.event_type == "model_reasoning_delta":
            reasoning_delta = _string(payload.get("reasoning_content")) or ""
            self.reasoning_text += reasoning_delta
        elif event.event_type == "model_message_completed":
            round_break = _positive_int(payload.get("tool_call_count"))
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
        elif event.event_type in {"run_completed", "run_aborted", "run_failed"}:
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
            round_break=round_break,
            pending_interaction=self.pending_interaction,
            interaction_changed=interaction_changed,
            diagnostic=diagnostic,
        )


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
        session_id=interaction.child_session_id,
        delegation_id=interaction.delegation_id,
        agent_name=interaction.agent_name,
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
