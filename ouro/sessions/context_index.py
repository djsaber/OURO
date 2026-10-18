"""Disposable file index for the small header of a lossless context snapshot."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ouro.core.base import OuroModel
from ouro.core.context_snapshots import ContextSnapshotHeader, ContextSnapshotRecord
from ouro.local_io import read_text_atomic


class ContextFileIndex(OuroModel):
    identity: tuple[int, int, int]
    header: ContextSnapshotHeader


def _identity(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def write_context_index(path: Path, record: ContextSnapshotRecord) -> None:
    """Called with the Session lock held, after the canonical atomic replacement."""
    try:
        index = ContextFileIndex(identity=_identity(path), header=record.header())
        path.with_name("context-index.json").write_text(index.model_dump_json(), encoding="utf-8")
    except OSError:
        # An interrupted or failed index write falls back to the canonical record.
        pass


def read_context_index(path: Path) -> ContextSnapshotHeader | None:
    """A missing or stale index requires a canonical read, never a stale status."""
    try:
        index = ContextFileIndex.model_validate_json(
            read_text_atomic(path.with_name("context-index.json"))
        )
        return index.header if index.identity == _identity(path) else None
    except OSError, ValidationError, ValueError:
        # This is a derived artifact. The caller still validates canonical data.
        return None
