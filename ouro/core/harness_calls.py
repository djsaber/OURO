from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Self

from pydantic import Field, model_validator

from ouro.core.base import OuroModel
from ouro.core.cancellation import AbortSignal
from ouro.core.harness import HarnessState
from ouro.core.session_controls import (
    RunControlBinding,
    SessionControlActivation,
    SessionControlState,
)
from ouro.core.session_extensions import SessionExtensionStateRecord


class HarnessCallSnapshot(OuroModel):
    """An operation's captured or staged state and its durable preconditions."""

    session_id: str
    harness_version: str
    history_epoch: int = Field(ge=0)
    last_message_id: str | None = None
    run_id: str | None = None
    state: HarnessState
    run_controls: tuple[RunControlBinding, ...] = ()
    # None means the application has no native usage ledger; an empty map means zero usage.
    usage_tokens: dict[str, dict[str, int]] | None = None

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        if any(item.session_id != self.session_id for item in self.state.states):
            raise ValueError("Harness call snapshot contains foreign Session state")
        return self


class HarnessStateChange(OuroModel):
    """One owner replacement, accepted together with its contribution's result."""

    session_id: str
    harness_version: str
    history_epoch: int = Field(ge=0)
    last_message_id: str | None = None
    owner_id: str
    state: SessionExtensionStateRecord | None
    # Only application control settlement supplies this field; SDK views stage owner state.
    controls: SessionControlState | None = None

    @model_validator(mode="after")
    def validate_owner(self) -> Self:
        if self.state is not None and (
            self.state.session_id != self.session_id or self.state.owner_id != self.owner_id
        ):
            raise ValueError("Harness proposal contains foreign owner state")
        return self


class HarnessInvocation:
    """Short-lived owner access; only its caller can accept the resulting proposal."""

    def __init__(
        self,
        snapshot: HarnessCallSnapshot,
        owner_id: str,
        *,
        writable: bool,
        signal: AbortSignal | None = None,
    ) -> None:
        self._session_id = snapshot.session_id
        self._version = snapshot.harness_version
        self._epoch = snapshot.history_epoch
        self._head = snapshot.last_message_id
        self._owner_id = owner_id
        self._before = next(
            (
                item.model_copy(deep=True)
                for item in snapshot.state.states
                if item.owner_id == owner_id
            ),
            None,
        )
        self._value = self._before
        self._controls = tuple(
            item.model_copy(deep=True)
            for item in snapshot.state.controls.activations
            if item.plugin_id == owner_id and item.phase == "active"
        )
        self._writable = writable
        self._run_controls = tuple(
            item.model_copy(deep=True)
            for item in snapshot.run_controls
            if item.owner_id == owner_id
        )
        self._usage_tokens = (
            dict(snapshot.usage_tokens.get(owner_id, {}))
            if snapshot.usage_tokens is not None
            else None
        )
        self._signal = signal
        self._closed = False
        # Synchronous callbacks and saved views may run in different threads.
        # Closing and staging are one local critical section, never a store lock.
        self._lock = threading.RLock()

    def read(self) -> SessionExtensionStateRecord | None:
        with self._lock:
            self._require_open()
            return self._value.model_copy(deep=True) if self._value is not None else None

    def controls(self) -> tuple[SessionControlActivation, ...]:
        with self._lock:
            self._require_open()
            return tuple(item.model_copy(deep=True) for item in self._controls)

    def replace(self, value: SessionExtensionStateRecord) -> None:
        with self._lock:
            self._require_write()
            if value.session_id != self._session_id or value.owner_id != self._owner_id:
                raise ValueError("Harness invocation cannot write another Session or owner")
            self._value = value.model_copy(deep=True)

    def run_controls(self) -> tuple[RunControlBinding, ...]:
        with self._lock:
            self._require_open()
            return tuple(item.model_copy(deep=True) for item in self._run_controls)

    def usage_tokens(self) -> dict[str, int] | None:
        with self._lock:
            self._require_open()
            return dict(self._usage_tokens) if self._usage_tokens is not None else None

    def delete(self) -> None:
        with self._lock:
            self._require_write()
            self._value = None

    def finish(self) -> HarnessStateChange | None:
        """Close a successful callback and return its proposal without saving it."""

        with self._lock:
            try:
                self._require_open()
                before = self._before.model_dump(exclude={"updated_at"}) if self._before else None
                after = self._value.model_dump(exclude={"updated_at"}) if self._value else None
                if before == after:
                    return None
                return HarnessStateChange(
                    session_id=self._session_id,
                    harness_version=self._version,
                    history_epoch=self._epoch,
                    last_message_id=self._head,
                    owner_id=self._owner_id,
                    state=self._value.model_copy(deep=True) if self._value is not None else None,
                )
            finally:
                self._closed = True

    def close(self) -> None:
        """Discard an unaccepted callback, including exceptional or timed-out work."""

        with self._lock:
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Harness invocation is closed")
        if self._signal is not None and self._signal.is_aborted():
            raise RuntimeError("Harness invocation was cancelled")

    def _require_write(self) -> None:
        self._require_open()
        if not self._writable:
            raise PermissionError("Harness invocation is read-only")


def accept_state_changes(
    snapshot: HarnessCallSnapshot, changes: Sequence[HarnessStateChange]
) -> HarnessCallSnapshot:
    """Merge accepted owners into the operation's draft, retaining its base version."""

    if not changes:
        return snapshot
    states = {item.owner_id: item for item in snapshot.state.states}
    owners: set[str] = set()
    controls = snapshot.state.controls
    control_changed = False
    for change in changes:
        if (
            change.session_id != snapshot.session_id
            or change.harness_version != snapshot.harness_version
            or change.history_epoch != snapshot.history_epoch
            or change.last_message_id != snapshot.last_message_id
        ):
            raise ValueError("Harness proposal does not match the operation's captured state")
        if change.owner_id in owners:
            raise ValueError("conflicting Harness proposals for the same owner")
        owners.add(change.owner_id)
        if change.controls is not None:
            if control_changed:
                raise ValueError("conflicting Harness control proposals")
            controls = change.controls
            control_changed = True
        if change.state is None:
            states.pop(change.owner_id, None)
        else:
            states[change.owner_id] = change.state
    return snapshot.model_copy(
        update={
            "state": HarnessState(
                controls=controls.model_copy(deep=True),
                states=tuple(item.model_copy(deep=True) for item in states.values()),
            )
        },
    )
