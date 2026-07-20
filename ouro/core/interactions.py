from __future__ import annotations

from typing import Annotated, Literal, cast, get_args

from pydantic import Field, JsonValue, model_validator

from ouro.core.base import OuroModel

SessionInteractionMode = Literal["interactive", "unattended"]
SESSION_INTERACTION_MODES = cast(
    tuple[SessionInteractionMode, ...],
    get_args(SessionInteractionMode),
)
InteractionResolvedBy = Literal["user", "host_policy"]


class AnswerResolution(OuroModel):
    """An opaque JSON answer supplied by a user or an identified Host policy."""

    kind: Literal["answer"] = "answer"
    value: JsonValue
    resolved_by: InteractionResolvedBy = "user"
    resolver_id: str | None = None

    @model_validator(mode="after")
    def validate_resolver(self) -> AnswerResolution:
        if self.resolved_by == "host_policy" and not self.resolver_id:
            raise ValueError("host policy answer requires resolver_id")
        if self.resolved_by == "user" and self.resolver_id is not None:
            raise ValueError("user answer cannot contain resolver_id")
        return self


class UnattendedResolution(OuroModel):
    """Tell a resumable tool that no user answer will be collected."""

    kind: Literal["unattended"] = "unattended"
    resolved_by: Literal["host_policy"] = "host_policy"
    resolver_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


ToolInputResolution = Annotated[
    AnswerResolution | UnattendedResolution,
    Field(discriminator="kind"),
]


__all__ = [
    "AnswerResolution",
    "InteractionResolvedBy",
    "SESSION_INTERACTION_MODES",
    "SessionInteractionMode",
    "ToolInputResolution",
    "UnattendedResolution",
]
