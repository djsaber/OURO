"""Rollbackable Goal business data and native execution/usage projections."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from ouro.sdk.plugins.v5 import (
    JsonValue,
    PluginRunControl,
    PluginSessionControl,
    PluginSessionStateView,
)

_STATE_SCHEMA_VERSION = 4

GoalStatus = Literal["active", "paused", "blocked", "budget_exhausted", "complete"]
UNFINISHED_GOAL_STATUSES: frozenset[GoalStatus] = frozenset(
    {"active", "paused", "blocked", "budget_exhausted"}
)


@dataclass(frozen=True)
class GoalRecord:
    goal_id: str
    objective: str
    status: GoalStatus
    token_budget: int | None
    created_at: str
    updated_at: str
    report: str | None

    @classmethod
    def from_json(cls, raw: object) -> GoalRecord:
        if not isinstance(raw, dict):
            raise ValueError("Goal record must be an object")
        status = raw.get("status")
        if status not in {*UNFINISHED_GOAL_STATUSES, "complete"}:
            raise ValueError(f"invalid Goal status: {status}")
        budget = raw.get("token_budget")
        if budget is not None and (
            isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0
        ):
            raise ValueError("Goal token_budget must be a positive integer")
        report = raw.get("report")
        return cls(
            goal_id=_required_text(raw.get("goal_id"), name="goal_id"),
            objective=_required_text(raw.get("objective"), name="objective"),
            status=status,
            token_budget=budget,
            created_at=_required_text(raw.get("created_at"), name="created_at"),
            updated_at=_required_text(raw.get("updated_at"), name="updated_at"),
            report=_required_text(report, name="report") if report is not None else None,
        )

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    def to_public(self, *, tokens_used: int | None) -> dict[str, object]:
        remaining = (
            max(self.token_budget - tokens_used, 0)
            if self.token_budget is not None and tokens_used is not None
            else None
        )
        public = self.to_json()
        del public["goal_id"]
        return {**public, "tokens_used": tokens_used, "tokens_remaining": remaining}


@dataclass(frozen=True)
class SessionGoalState:
    goal: GoalRecord | None = None

    @classmethod
    def from_json(cls, raw: object) -> SessionGoalState:
        if not isinstance(raw, dict):
            raise ValueError("Goal state must be an object")
        goal_raw = raw.get("goal")
        return cls(goal=GoalRecord.from_json(goal_raw) if goal_raw is not None else None)

    def to_json(self) -> dict[str, object]:
        return {"goal": self.goal.to_json() if self.goal is not None else None}


class GoalStateRepository:
    """Access one call's Goal draft and the framework-owned facts beside it."""

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

    def control(self) -> PluginSessionControl | None:
        return next((item for item in self._state.controls() if item.controller_id == "mode"), None)

    def run_control(self) -> PluginRunControl | None:
        return next(
            (item for item in self._state.run_controls() if item.controller_id == "mode"), None
        )

    def tokens_used(self, goal: GoalRecord) -> int | None:
        totals = self._state.usage_tokens()
        return totals.get(goal.goal_id, 0) if totals is not None else None

    def save(self, state: SessionGoalState) -> None:
        self._state.replace(
            state_schema_version=_STATE_SCHEMA_VERSION,
            payload=cast(JsonValue, state.to_json()),
            branch_policy="reset",
            projection_semantic_tags=(
                "goal-create",
                "goal-continue",
                "goal-updated",
                "goal-cancelled",
            ),
        )

    def delete(self) -> None:
        self._state.delete()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Goal {name} must be non-empty text")
    return value


__all__ = [
    "GoalRecord",
    "GoalStateRepository",
    "GoalStatus",
    "SessionGoalState",
    "UNFINISHED_GOAL_STATUSES",
    "utc_now",
]
