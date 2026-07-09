from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord


class SessionSnapshot(OuroModel):
    session: SessionRecord
    messages: list[MessageRecord] = Field(default_factory=list)
    events: list[EventRecord] = Field(default_factory=list)
    runs: list[RunRecord] = Field(default_factory=list)


class SessionStore(Protocol):
    root: Path
    sessions_dir: Path

    def initialize(self) -> None: ...

    def create_session(self, session: SessionRecord) -> None: ...

    def save_session(self, session: SessionRecord) -> None: ...

    def append_message(self, message: MessageRecord) -> None: ...

    def append_event(self, event: EventRecord) -> None: ...

    def save_run(self, run: RunRecord) -> None: ...

    def load_session(self, session_id: str, *, recover: bool = True) -> SessionSnapshot: ...

    def list_sessions(self) -> list[SessionRecord]: ...

    def rebuild_index(self) -> None: ...

    def load_index(self) -> dict[str, object]: ...

    def acquire_session_lock(self, session_id: str, run_id: str) -> object | None: ...

    def is_session_locked(self, session_id: str) -> bool: ...
