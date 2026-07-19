"""Session-scoped state and private persistence for the builtin plan mode plugin."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from ouro.sdk.plugins.v1 import JsonValue, PluginDataStore

_STATE_SCHEMA_VERSION = 1
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

PlanPhase = Literal["inactive", "active", "awaiting_approval"]


@dataclass(frozen=True)
class PlanOption:
    label: str
    description: str = ""

    def to_json(self) -> dict[str, JsonValue]:
        return {"label": self.label, "description": self.description}


@dataclass(frozen=True)
class SessionPlanState:
    phase: PlanPhase = "inactive"
    plan_path: str | None = None
    revision: int = 0
    activation_tool_call_id: str | None = None
    approval_tool_call_id: str | None = None
    approval_digest: str | None = None
    options: tuple[PlanOption, ...] = ()
    last_resume_tool_call_id: str | None = None
    last_resume_output: dict[str, object] | None = None

    @classmethod
    def from_json(cls, raw: object) -> SessionPlanState:
        if not isinstance(raw, dict):
            raise ValueError("plan mode Session state must be an object")
        phase = raw.get("phase", "inactive")
        if phase not in {"inactive", "active", "awaiting_approval"}:
            raise ValueError(f"invalid plan mode phase: {phase}")
        plan_path = raw.get("plan_path")
        if plan_path is not None and not isinstance(plan_path, str):
            raise ValueError("plan mode plan_path must be a string")
        revision = raw.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("plan mode revision must be a non-negative integer")
        options_raw = raw.get("options", [])
        if not isinstance(options_raw, list):
            raise ValueError("plan mode options must be a list")
        options: list[PlanOption] = []
        for item in options_raw:
            if not isinstance(item, dict) or not isinstance(item.get("label"), str):
                raise ValueError("plan mode option must contain a string label")
            description = item.get("description", "")
            if not isinstance(description, str):
                raise ValueError("plan mode option description must be a string")
            options.append(PlanOption(label=item["label"], description=description))
        last_output = raw.get("last_resume_output")
        if last_output is not None and not isinstance(last_output, dict):
            raise ValueError("plan mode last resume output must be an object")
        return cls(
            phase=phase,
            plan_path=plan_path,
            revision=revision,
            activation_tool_call_id=_optional_string(raw.get("activation_tool_call_id")),
            approval_tool_call_id=_optional_string(raw.get("approval_tool_call_id")),
            approval_digest=_optional_string(raw.get("approval_digest")),
            options=tuple(options),
            last_resume_tool_call_id=_optional_string(raw.get("last_resume_tool_call_id")),
            last_resume_output=dict(last_output) if last_output is not None else None,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "plan_path": self.plan_path,
            "revision": self.revision,
            "activation_tool_call_id": self.activation_tool_call_id,
            "approval_tool_call_id": self.approval_tool_call_id,
            "approval_digest": self.approval_digest,
            "options": [option.to_json() for option in self.options],
            "last_resume_tool_call_id": self.last_resume_tool_call_id,
            "last_resume_output": self.last_resume_output,
        }


class PlanStateRepository:
    """Persist each Session independently so Generation overlap cannot clobber peers."""

    def __init__(self, data: PluginDataStore) -> None:
        self._data = data

    def load(self, session_id: str) -> SessionPlanState:
        path = self._session_path(session_id)
        try:
            raw = json.loads(self._data.read_bytes(path).decode("utf-8"))
        except FileNotFoundError:
            return SessionPlanState()
        if not isinstance(raw, dict) or raw.get("schema_version") != _STATE_SCHEMA_VERSION:
            raise ValueError("unsupported plan mode state schema")
        return SessionPlanState.from_json(raw.get("state"))

    def save(self, session_id: str, state: SessionPlanState) -> None:
        payload = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "state": state.to_json(),
        }
        self._data.write_bytes(
            self._session_path(session_id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
        )

    @staticmethod
    def _session_path(session_id: str) -> str:
        return f"sessions/{validate_session_id(session_id)}.json"


def validate_session_id(session_id: str) -> str:
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("Session id cannot be used as a plan mode data path")
    return session_id


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
