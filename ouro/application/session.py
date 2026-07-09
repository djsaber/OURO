from __future__ import annotations

from ouro.core.state import SessionRecord
from ouro.persistence import SessionSnapshot, SessionStore


class SessionService:
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def create(self, *, title: str = "New session", workspace: str | None = None) -> SessionRecord:
        session = SessionRecord(title=title, workspace=workspace)
        self.store.create_session(session)
        return session

    def open(self, session_id: str) -> SessionSnapshot:
        return self.store.load_session(session_id)

    def list(self) -> list[SessionRecord]:
        return self.store.list_sessions()
