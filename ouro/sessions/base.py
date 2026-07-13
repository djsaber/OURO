from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Protocol

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionRecord
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.index import SessionIndex


class SessionSnapshot(OuroModel):
    session: SessionRecord
    messages: list[MessageRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    runs: list[RunRecord] = Field(default_factory=list)
    compactions: list[CompactionRecord] = Field(default_factory=list)

    @property
    def active_compaction(self) -> CompactionRecord | None:
        active_id = self.session.active_compaction_id
        if active_id is None:
            return None
        for compaction in self.compactions:
            if compaction.compaction_id == active_id:
                return compaction
        return None


class SessionLock(Protocol):
    def release(self) -> None: ...


class SessionStore(Protocol):
    root: Path
    sessions_dir: Path

    def initialize(self) -> None: ...

    def create_session(self, session: SessionRecord) -> None: ...

    def create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None = None,
    ) -> None: ...

    def save_session(self, session: SessionRecord) -> None: ...

    def trash_session(self, session_id: str) -> str: ...

    def append_message(self, message: MessageRecord) -> None: ...

    def append_event(self, event: EventRecord) -> None: ...

    def save_run(self, run: RunRecord) -> None: ...

    def append_compaction(self, compaction: CompactionRecord) -> None: ...

    def activate_compaction(self, session_id: str, compaction_id: str) -> SessionRecord: ...

    def load_session(self, session_id: str, *, recover: bool = True) -> SessionSnapshot: ...

    def load_session_record(self, session_id: str) -> SessionRecord: ...

    def load_run(self, session_id: str, run_id: str) -> RunRecord: ...

    def list_sessions(self) -> list[SessionRecord]: ...

    def scan_index(self) -> SessionIndex: ...

    def rebuild_index(self) -> SessionIndex: ...

    def try_rebuild_index(self) -> str | None: ...

    def try_update_index(self, session_id: str) -> str | None: ...

    def load_index(self) -> SessionIndex: ...

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionLock | None: ...

    def is_session_locked(self, session_id: str) -> bool: ...


def session_mutation(
    store: SessionStore,
    session_id: str,
) -> AbstractContextManager[object, None]:
    """Use the optional short mutation boundary without breaking existing stores."""

    mutation = getattr(store, "session_mutation", None)
    if callable(mutation):
        return mutation(session_id)
    return nullcontext()


def append_ordered_event(store: SessionStore, event: EventRecord) -> EventRecord:
    """Use store-owned sequence allocation when the persistence backend supports it."""

    append = getattr(store, "append_ordered_event", None)
    if callable(append):
        return append(event)
    store.append_event(event)
    return event
