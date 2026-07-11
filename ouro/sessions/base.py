from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionRecord
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord


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

    def save_session(self, session: SessionRecord) -> None: ...

    def append_message(self, message: MessageRecord) -> None: ...

    def append_event(self, event: EventRecord) -> None: ...

    def save_run(self, run: RunRecord) -> None: ...

    def append_compaction(self, compaction: CompactionRecord) -> None: ...

    def activate_compaction(self, session_id: str, compaction_id: str) -> SessionRecord: ...

    def load_session(self, session_id: str, *, recover: bool = True) -> SessionSnapshot: ...

    def list_sessions(self) -> list[SessionRecord]: ...

    def rebuild_index(self) -> None: ...

    def load_index(self) -> dict[str, object]: ...

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionLock | None: ...

    def is_session_locked(self, session_id: str) -> bool: ...
