"""Session-scoped private state for the builtin Goal mode plugin."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ouro.sdk.plugins.v1 import JsonValue, PluginDataStore

_STATE_SCHEMA_VERSION = 2
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_GOAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

GoalStatus = Literal["active", "paused", "blocked", "complete"]
GoalActor = Literal["user", "model", "runtime"]
UNFINISHED_GOAL_STATUSES: frozenset[GoalStatus] = frozenset(
    {"active", "paused", "blocked"}
)


@dataclass(frozen=True)
class GoalContract:
    outcome: str
    verification: tuple[str, ...]
    constraints: tuple[str, ...]
    boundaries: tuple[str, ...]
    iteration_strategy: str
    blocked_conditions: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: object) -> GoalContract:
        if not isinstance(raw, dict):
            raise ValueError("Goal contract must be an object")
        return cls(
            outcome=_required_text(raw.get("outcome"), name="contract outcome"),
            verification=_required_text_tuple(
                raw.get("verification"),
                name="contract verification",
                allow_empty=False,
            ),
            constraints=_required_text_tuple(
                raw.get("constraints"),
                name="contract constraints",
                allow_empty=True,
            ),
            boundaries=_required_text_tuple(
                raw.get("boundaries"),
                name="contract boundaries",
                allow_empty=True,
            ),
            iteration_strategy=_required_text(
                raw.get("iteration_strategy"),
                name="contract iteration_strategy",
            ),
            blocked_conditions=_required_text_tuple(
                raw.get("blocked_conditions"),
                name="contract blocked_conditions",
                allow_empty=False,
            ),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "outcome": self.outcome,
            "verification": list(self.verification),
            "constraints": list(self.constraints),
            "boundaries": list(self.boundaries),
            "iteration_strategy": self.iteration_strategy,
            "blocked_conditions": list(self.blocked_conditions),
        }


@dataclass(frozen=True)
class GoalRecord:
    goal_id: str
    source_request: str
    contract: GoalContract
    status: GoalStatus
    actor: GoalActor
    revision: int
    activation_id: str | None
    token_budget: int | None
    tokens_used: int
    runs_used: int
    last_accounted_run_id: str | None
    reason_code: str | None
    report: str | None
    created_at: str
    updated_at: str
    status_changed_at: str

    @classmethod
    def from_json(cls, raw: object) -> GoalRecord:
        if not isinstance(raw, dict):
            raise ValueError("Goal record must be an object")
        goal_id = _required_identifier(raw.get("goal_id"), name="goal_id")
        source_request = _required_text(raw.get("source_request"), name="source_request")
        contract = GoalContract.from_json(raw.get("contract"))
        status = raw.get("status")
        if status not in {"active", "paused", "blocked", "complete"}:
            raise ValueError(f"invalid Goal status: {status}")
        actor = raw.get("actor")
        if actor not in {"user", "model", "runtime"}:
            raise ValueError(f"invalid Goal actor: {actor}")
        revision = _non_negative_int(raw.get("revision"), name="revision")
        activation_id = _optional_identifier(raw.get("activation_id"), name="activation_id")
        if status == "active" and activation_id is None:
            raise ValueError("active Goal requires activation_id")
        if status != "active" and activation_id is not None:
            raise ValueError("inactive Goal cannot retain activation_id")
        token_budget = _optional_positive_int(raw.get("token_budget"), name="token_budget")
        tokens_used = _non_negative_int(raw.get("tokens_used"), name="tokens_used")
        runs_used = _non_negative_int(raw.get("runs_used"), name="runs_used")
        return cls(
            goal_id=goal_id,
            source_request=source_request,
            contract=contract,
            status=status,
            actor=actor,
            revision=revision,
            activation_id=activation_id,
            token_budget=token_budget,
            tokens_used=tokens_used,
            runs_used=runs_used,
            last_accounted_run_id=_optional_identifier(
                raw.get("last_accounted_run_id"), name="last_accounted_run_id"
            ),
            reason_code=_optional_text(raw.get("reason_code"), name="reason_code"),
            report=_optional_text(raw.get("report"), name="report"),
            created_at=_timestamp(raw.get("created_at"), name="created_at"),
            updated_at=_timestamp(raw.get("updated_at"), name="updated_at"),
            status_changed_at=_timestamp(raw.get("status_changed_at"), name="status_changed_at"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "goal_id": self.goal_id,
            "source_request": self.source_request,
            "contract": self.contract.to_json(),
            "status": self.status,
            "actor": self.actor,
            "revision": self.revision,
            "activation_id": self.activation_id,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "runs_used": self.runs_used,
            "last_accounted_run_id": self.last_accounted_run_id,
            "reason_code": self.reason_code,
            "report": self.report,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status_changed_at": self.status_changed_at,
        }

    def to_public(self) -> dict[str, object]:
        remaining = (
            None if self.token_budget is None else max(self.token_budget - self.tokens_used, 0)
        )
        return {**self.to_json(), "tokens_remaining": remaining}


@dataclass(frozen=True)
class GoalRunBinding:
    run_id: str
    goal_id: str
    goal_revision: int
    activation_id: str

    @classmethod
    def from_json(cls, raw: object) -> GoalRunBinding:
        if not isinstance(raw, dict):
            raise ValueError("Goal run binding must be an object")
        return cls(
            run_id=_required_identifier(raw.get("run_id"), name="run_id"),
            goal_id=_required_identifier(raw.get("goal_id"), name="goal_id"),
            goal_revision=_non_negative_int(raw.get("goal_revision"), name="goal_revision"),
            activation_id=_required_identifier(raw.get("activation_id"), name="activation_id"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "activation_id": self.activation_id,
        }


@dataclass(frozen=True)
class SessionGoalState:
    goal: GoalRecord | None = None
    run_binding: GoalRunBinding | None = None

    @classmethod
    def from_json(cls, raw: object) -> SessionGoalState:
        if not isinstance(raw, dict):
            raise ValueError("Goal Session state must be an object")
        goal_raw = raw.get("goal")
        binding_raw = raw.get("run_binding")
        return cls(
            goal=GoalRecord.from_json(goal_raw) if goal_raw is not None else None,
            run_binding=(
                GoalRunBinding.from_json(binding_raw) if binding_raw is not None else None
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "goal": self.goal.to_json() if self.goal is not None else None,
            "run_binding": (self.run_binding.to_json() if self.run_binding is not None else None),
        }


class GoalStateRepository:
    """Persist one independent Goal record per Session."""

    def __init__(self, data: PluginDataStore) -> None:
        self._data = data

    def load(self, session_id: str) -> SessionGoalState:
        path = self._session_path(session_id)
        try:
            raw = json.loads(self._data.read_bytes(path).decode("utf-8"))
        except FileNotFoundError:
            return SessionGoalState()
        if not isinstance(raw, dict) or raw.get("schema_version") != _STATE_SCHEMA_VERSION:
            raise ValueError("unsupported Goal mode state schema")
        return SessionGoalState.from_json(raw.get("state"))

    def save(self, session_id: str, state: SessionGoalState) -> None:
        payload = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "state": state.to_json(),
        }
        self._data.write_bytes(
            self._session_path(session_id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
        )

    def delete(self, session_id: str) -> None:
        try:
            self._data.delete(self._session_path(session_id))
        except FileNotFoundError:
            return

    @staticmethod
    def _session_path(session_id: str) -> str:
        return f"sessions/{validate_session_id(session_id)}.json"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def validate_session_id(session_id: str) -> str:
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("Session id cannot be used as a Goal mode data path")
    return session_id


def _required_identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _GOAL_ID.fullmatch(value) is None:
        raise ValueError(f"Goal {name} must be a stable identifier")
    return value


def _optional_identifier(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _required_identifier(value, name=name)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Goal {name} must be non-empty text")
    return value


def _optional_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name=name)


def _required_text_tuple(
    value: object,
    *,
    name: str,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Goal {name} must be a list")
    if not allow_empty and not value:
        raise ValueError(f"Goal {name} must not be empty")
    return tuple(_required_text(item, name=f"{name} item") for item in value)


def _non_negative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Goal {name} must be a non-negative integer")
    return value


def _optional_positive_int(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Goal {name} must be a positive integer")
    return value


def _timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Goal {name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Goal {name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Goal {name} must include a timezone")
    return value


__all__ = [
    "GoalActor",
    "GoalContract",
    "GoalRecord",
    "GoalRunBinding",
    "GoalStateRepository",
    "GoalStatus",
    "SessionGoalState",
    "UNFINISHED_GOAL_STATUSES",
    "utc_now",
    "validate_session_id",
]
