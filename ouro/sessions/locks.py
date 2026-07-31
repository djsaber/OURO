from __future__ import annotations

from pathlib import Path

from ouro.local_locks import (
    FileMutationLock,
    FileOwnerLock,
)


class SessionRunLock(FileOwnerLock):
    """Cross-process lock for one locally persisted session run."""

    def __init__(self, path: Path, *, session_id: str, run_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self.run_id = run_id
        super().__init__(
            path,
            owner={"session_id": session_id, "run_id": run_id},
        )


class SessionMutationLock(FileMutationLock):
    """File mutation lock carrying the owning Session identity for diagnostics."""

    def __init__(self, path: Path, *, session_id: str) -> None:
        super().__init__(path)
        self.session_id = session_id
