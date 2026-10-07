from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from ouro.core.resources import ResourceRecord, ResourceSource


class ResourceStore(Protocol):
    """Named immutable files; canonical metadata belongs to the Session repository."""

    def infer_media_type(self, path: Path) -> str: ...

    def import_file(
        self,
        session_id: str,
        source_path: Path,
        *,
        source: ResourceSource,
        media_type: str,
        max_bytes: int,
    ) -> ResourceRecord: ...

    def import_contents(
        self, session_id: str, items: Iterable[tuple[ResourceRecord, bytes]]
    ) -> None: ...

    def directory_for(self, session_id: str) -> Path: ...

    def path_for(self, session_id: str, record: ResourceRecord) -> Path: ...

    def load_content(
        self,
        session_id: str,
        record: ResourceRecord,
        *,
        max_bytes: int | None = None,
    ) -> bytes: ...

    def verify(self, session_id: str, record: ResourceRecord) -> None: ...

    def export_file(
        self, session_id: str, record: ResourceRecord, destination: Path, *, overwrite: bool
    ) -> Path: ...

    def read_ranges(
        self, session_id: str, record: ResourceRecord, ranges: tuple[tuple[int, int], ...]
    ) -> tuple[bytes, ...]: ...

    def delete_resources(self, session_id: str, records: Iterable[ResourceRecord]) -> list[str]: ...
