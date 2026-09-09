"""Session-scoped canonical state for the builtin Goal plugin."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from ouro.sdk.plugins.v2 import JsonValue, PluginSessionStateView

_STATE_SCHEMA_VERSION = 3
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

GoalStatus = Literal[
    "active",
    "paused",
    "blocked",
    "budget_exhausted",
    "complete",
]
UNFINISHED_GOAL_STATUSES: frozenset[GoalStatus] = frozenset(
    {"active", "paused", "blocked", "budget_exhausted"}
)


@dataclass(frozen=True)
class GoalRecord:
    objective: str
    status: GoalStatus
    token_budget: int | None
    tokens_used: int
    created_at: str
    updated_at: str
    report: str | None
    activation_id: str | None
    revision: int
    last_accounted_run_id: str | None

    @classmethod
    def from_json(cls, raw: object) -> GoalRecord:
        if not isinstance(raw, dict):
            raise ValueError("Goal record must be an object")
        status = raw.get("status")
        if status not in {
            "active",
            "paused",
            "blocked",
            "budget_exhausted",
            "complete",
        }:
            raise ValueError(f"invalid Goal status: {status}")
        activation_id = _optional_identifier(raw.get("activation_id"), name="activation_id")
        if status == "complete" and activation_id is not None:
            raise ValueError("completed Goal cannot retain an activation")
        if status != "complete" and activation_id is None:
            raise ValueError("unfinished Goal requires an activation")
        token_budget = _optional_positive_int(raw.get("token_budget"), name="token_budget")
        tokens_used = _non_negative_int(raw.get("tokens_used"), name="tokens_used")
        return cls(
            objective=_required_text(raw.get("objective"), name="objective"),
            status=status,
            token_budget=token_budget,
            tokens_used=tokens_used,
            created_at=_timestamp(raw.get("created_at"), name="created_at"),
            updated_at=_timestamp(raw.get("updated_at"), name="updated_at"),
            report=_optional_text(raw.get("report"), name="report"),
            activation_id=activation_id,
            revision=_positive_int(raw.get("revision"), name="revision"),
            last_accounted_run_id=_optional_identifier(
                raw.get("last_accounted_run_id"),
                name="last_accounted_run_id",
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "status": self.status,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "report": self.report,
            "activation_id": self.activation_id,
            "revision": self.revision,
            "last_accounted_run_id": self.last_accounted_run_id,
        }

    def to_public(self) -> dict[str, object]:
        remaining = (
            None if self.token_budget is None else max(self.token_budget - self.tokens_used, 0)
        )
        return {
            "objective": self.objective,
            "status": self.status,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "tokens_remaining": remaining,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "report": self.report,
        }


@dataclass(frozen=True)
class GoalRunBinding:
    run_id: str
    activation_id: str
    goal_revision: int

    @classmethod
    def from_json(cls, raw: object) -> GoalRunBinding:
        if not isinstance(raw, dict):
            raise ValueError("Goal execution binding must be an object")
        return cls(
            run_id=_required_identifier(raw.get("run_id"), name="run_id"),
            activation_id=_required_identifier(
                raw.get("activation_id"),
                name="activation_id",
            ),
            goal_revision=_positive_int(raw.get("goal_revision"), name="goal_revision"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "activation_id": self.activation_id,
            "goal_revision": self.goal_revision,
        }


@dataclass(frozen=True)
class SessionGoalState:
    goal: GoalRecord | None = None
    run_binding: GoalRunBinding | None = None

    @classmethod
    def from_json(cls, raw: object) -> SessionGoalState:
        if not isinstance(raw, dict):
            raise ValueError("Goal state must be an object")
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
            "run_binding": self.run_binding.to_json() if self.run_binding is not None else None,
        }


class GoalStateRepository:
    """Persist the latest Goal slot for one conversation."""

    def __init__(self, state: PluginSessionStateView | None) -> None:
        if state is None:
            raise RuntimeError("Session state requires an active plugin invocation")
        self._state = state

    def load(self) -> SessionGoalState:
        persisted = self._state.read()
        if persisted is None:
            return SessionGoalState()
        if persisted.state_schema_version != _STATE_SCHEMA_VERSION:
            raise ValueError("unsupported Goal state schema")
        return SessionGoalState.from_json(persisted.payload)

    def save(self, state: SessionGoalState) -> None:
        self._state.replace(
            state_schema_version=_STATE_SCHEMA_VERSION,
            payload=cast(JsonValue, state.to_json()),
            branch_policy="reset",
            projection_semantic_tags=(
                "goal-create",
                "goal-continue",
                "goal-paused",
                "goal-resumed",
                "goal-updated",
                "goal-cancelled",
            ),
        )

    def delete(self) -> None:
        self._state.delete()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def validate_session_id(session_id: str) -> str:
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("Session id cannot be used as Goal data")
    return session_id


def _required_identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
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


def _non_negative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Goal {name} must be a non-negative integer")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Goal {name} must be a positive integer")
    return value


def _optional_positive_int(value: object, *, name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, name=name)


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
    "GoalRecord",
    "GoalRunBinding",
    "GoalStateRepository",
    "GoalStatus",
    "SessionGoalState",
    "UNFINISHED_GOAL_STATUSES",
    "utc_now",
    "validate_session_id",
]
