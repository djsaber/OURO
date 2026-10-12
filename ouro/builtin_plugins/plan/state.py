"""Plan business data; mode and pending interaction ownership belong to OURO."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from ouro.sdk.plugins.v3 import JsonValue, PluginSessionStateView

STATE_SCHEMA_VERSION = 2
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


@dataclass(frozen=True)
class SessionPlanState:
    plan_path: str | None = None

    @classmethod
    def from_json(cls, raw: object) -> SessionPlanState:
        if not isinstance(raw, dict):
            raise ValueError("plan mode Session state must be an object")
        plan_path = raw.get("plan_path")
        if plan_path is not None and not isinstance(plan_path, str):
            raise ValueError("plan mode plan_path must be a string")
        return cls(plan_path=plan_path)

    def to_json(self) -> dict[str, object]:
        return {"plan_path": self.plan_path}


class PlanStateRepository:
    def __init__(self, state: PluginSessionStateView | None) -> None:
        if state is None:
            raise RuntimeError("Session state requires an active plugin invocation")
        self._state = state

    @property
    def active(self) -> bool:
        return any(item.controller_id == "mode" for item in self._state.controls())

    def load(self) -> SessionPlanState:
        persisted = self._state.read()
        if persisted is None:
            return SessionPlanState()
        if persisted.state_schema_version != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported plan mode state schema")
        return SessionPlanState.from_json(persisted.payload)

    def save(self, state: SessionPlanState) -> None:
        self._state.replace(
            state_schema_version=STATE_SCHEMA_VERSION,
            payload=cast(JsonValue, state.to_json()),
            branch_policy="reset",
            projection_semantic_tags=("plan-context",),
        )


def validate_session_id(session_id: str) -> str:
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("Session id cannot be used as a plan mode data path")
    return session_id
