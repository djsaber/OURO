from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from ouro.core.resources import ResourceRecord, ResourceSource, SessionResourceReference


class ResourceStore(Protocol):
    """Persistence port for immutable resources and Session reference records."""

    root: Path

    def initialize(self) -> None: ...

    def infer_media_type(self, path: Path) -> str: ...

    def import_file(
        self,
        session_id: str,
        source_path: Path,
        *,
        source: ResourceSource,
        media_type: str,
        max_bytes: int,
    ) -> tuple[ResourceRecord, bool]: ...

    def load_record(self, resource_id: str, *, verify: bool = False) -> ResourceRecord: ...

    def load_content(
        self,
        resource_id: str,
        *,
        max_bytes: int | None = None,
    ) -> tuple[ResourceRecord, bytes]: ...

    def list_session_records(self, session_id: str) -> list[ResourceRecord]: ...

    def load_session_references(self, session_id: str) -> list[SessionResourceReference]: ...

    def remove_session_reference(self, session_id: str, resource_id: str) -> None: ...

    def export_file(self, resource_id: str, destination: Path, *, overwrite: bool) -> Path: ...

    def collect_unreferenced(self, resource_ids: Iterable[str]) -> list[str]: ...

    def rebuild_index(self) -> None: ...

    def try_update_index(self) -> str | None: ...
