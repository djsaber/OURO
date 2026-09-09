from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from ouro.core.base import OuroModel, VersionedOuroModel, new_id
from ouro.core.session_controls import SessionControlState
from ouro.core.session_extensions import SessionExtensionStateRecord


class HarnessState(OuroModel):
    """Revertible Session business state, independent of execution resources."""

    controls: SessionControlState = Field(default_factory=SessionControlState)
    states: tuple[SessionExtensionStateRecord, ...] = ()

    @model_validator(mode="after")
    def validate_owners(self) -> Self:
        owners = [state.owner_id for state in self.states]
        if len(owners) != len(set(owners)):
            raise ValueError("Harness state cannot contain duplicate owners")
        if len({state.session_id for state in self.states}) > 1:
            raise ValueError("Harness state cannot mix Sessions")
        self.states = tuple(sorted(self.states, key=lambda state: state.owner_id))
        return self

    def same_value(self, other: HarnessState) -> bool:
        """Storage timestamps do not create a new business-state version."""

        def values(state: HarnessState) -> object:
            return (
                state.controls.model_dump(exclude={"updated_at"}),
                [item.model_dump(exclude={"updated_at"}) for item in state.states],
            )

        return values(self) == values(other)


class HarnessVersion(VersionedOuroModel):
    """An immutable, Session-owned checkpoint referenced by messages."""

    session_id: str
    version_id: str = Field(
        default_factory=lambda: new_id("harness"), pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    )
    state: HarnessState = Field(default_factory=HarnessState)

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        if any(item.session_id != self.session_id for item in self.state.states):
            raise ValueError("Harness version contains state from another Session")
        return self
