from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ouro.core.base import OuroModel, new_id, utc_now

SessionInputMode = Literal["follow_up", "steer"]
SessionControlAccess = Literal["exclusive", "shared"]
SessionControlPhase = Literal["activating", "active", "deactivating", "failed"]
SessionControlTransitionAction = Literal["acquire", "release", "replace"]

_CONTROL_DOMAIN = re.compile(
    r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+\Z",
    re.IGNORECASE,
)
_LOCAL_CONTROLLER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class SessionInputRequest(OuroModel):
    """A request for the application layer to add one session input.

    Extensions describe the requested input, while the application runtime
    remains the only owner of persistence, locking, and execution.
    """

    prompt: str = Field(min_length=1)
    mode: SessionInputMode = "follow_up"
    title: str | None = None

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session input prompt cannot be blank")
        return value


class AttributedSessionInput(SessionInputRequest):
    """A request bound to the extension generation that produced it."""

    plugin_id: str
    contribution_id: str
    generation_id: str
    execution_contract_hash: str


class HostSessionInput(SessionInputRequest):
    """A synthetic input produced by an OURO Host/Application component."""

    host_source_id: str = Field(min_length=1)
    queue_id: str | None = Field(default=None, min_length=1, max_length=256)
    resource_refs: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("host_source_id")
    @classmethod
    def validate_host_source_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("host session input source id cannot be blank")
        return normalized

    @field_validator("resource_refs")
    @classmethod
    def validate_resource_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("Host resource references cannot be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Host resource references must be unique")
        return normalized


class SessionControlClaim(OuroModel):
    """One declared Session resource claim used for activation-time arbitration."""

    domain: str
    access: SessionControlAccess = "exclusive"

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        normalized = value.strip().lower()
        if _CONTROL_DOMAIN.fullmatch(normalized) is None:
            raise ValueError("session control domain must be a dotted stable identifier")
        return normalized


class SessionControlTransitionRequest(OuroModel):
    """Unattributed control intent emitted by an extension callback."""

    action: SessionControlTransitionAction
    controller_id: str | None = None
    activation_id: str | None = None
    replace_activation_ids: list[str] = Field(default_factory=list)
    expected_session_revision: int | None = Field(default=None, ge=0)
    expected_activation_revision: int | None = Field(default=None, ge=0)
    payload: JsonValue = None
    reason: str | None = None

    @field_validator("controller_id")
    @classmethod
    def validate_controller_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if _LOCAL_CONTROLLER_ID.fullmatch(normalized) is None:
            raise ValueError("session controller id must be a lowercase stable identifier")
        return normalized

    @field_validator("activation_id")
    @classmethod
    def validate_activation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("session control activation id cannot be blank")
        return normalized

    @field_validator("replace_activation_ids")
    @classmethod
    def validate_replace_activation_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("replace activation ids cannot be blank")
        return list(dict.fromkeys(normalized))

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_action_shape(self) -> Self:
        if self.action == "acquire":
            if self.controller_id is None:
                raise ValueError("acquire transition requires controller_id")
            if self.activation_id is not None or self.replace_activation_ids:
                raise ValueError("acquire transition cannot select existing activations")
        elif self.action == "release":
            if self.controller_id is None and self.activation_id is None:
                raise ValueError("release transition requires controller_id or activation_id")
            if self.replace_activation_ids:
                raise ValueError("release transition cannot replace activations")
        else:
            if self.controller_id is None or not self.replace_activation_ids:
                raise ValueError(
                    "replace transition requires controller_id and replace_activation_ids"
                )
            if self.activation_id is not None:
                raise ValueError("replace transition cannot specify activation_id")
        return self


class AttributedSessionControlTransition(SessionControlTransitionRequest):
    """Control intent bound to the exact extension generation that produced it."""

    plugin_id: str
    generation_id: str
    execution_contract_hash: str


class SessionControlActivation(OuroModel):
    """Durable Host-owned activation of one plugin Session controller."""

    activation_id: str = Field(default_factory=lambda: new_id("activation"))
    controller_id: str
    plugin_id: str
    controller_local_id: str
    plugin_class: Literal["builtin", "user"]
    release_id: str
    generation_id: str
    execution_contract_hash: str
    claims: list[SessionControlClaim] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)
    phase: SessionControlPhase = "active"
    owner_state_revision: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        expected = f"{self.plugin_id}/session_controllers/{self.controller_local_id}"
        if self.controller_id != expected:
            raise ValueError("session control activation canonical identity is inconsistent")
        domains = [claim.domain for claim in self.claims]
        if len(domains) != len(set(domains)):
            raise ValueError("session control activation contains duplicate claim domains")
        return self


class SessionControlState(OuroModel):
    """Canonical Session projection for all active or transitioning controllers."""

    revision: int = Field(default=0, ge=0)
    activations: list[SessionControlActivation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_activations(self) -> Self:
        activation_ids = [item.activation_id for item in self.activations]
        if len(activation_ids) != len(set(activation_ids)):
            raise ValueError("duplicate session control activation id")
        controller_ids = [item.controller_id for item in self.activations if item.phase != "failed"]
        if len(controller_ids) != len(set(controller_ids)):
            raise ValueError("duplicate active session controller identity")
        return self


class SessionControlTransitionResult(OuroModel):
    action: SessionControlTransitionAction
    changed: bool
    session_revision: int = Field(ge=0)
    activation: SessionControlActivation | None = None
    released_activation_ids: list[str] = Field(default_factory=list)
    output: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AttributedSessionInput",
    "AttributedSessionControlTransition",
    "HostSessionInput",
    "SessionControlAccess",
    "SessionControlActivation",
    "SessionControlClaim",
    "SessionControlPhase",
    "SessionControlState",
    "SessionControlTransitionAction",
    "SessionControlTransitionRequest",
    "SessionControlTransitionResult",
    "SessionInputMode",
    "SessionInputRequest",
]
