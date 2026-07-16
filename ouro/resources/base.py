from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from ouro.core.resources import ResourceRecord, ResourceSource


class BlobStore(Protocol):
    """Persistence port for immutable content, independent from Session ownership."""

    def infer_media_type(self, path: Path) -> str: ...

    def import_file(
        self,
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

    def export_file(self, resource_id: str, destination: Path, *, overwrite: bool) -> Path: ...

    def delete_resources(self, resource_ids: Iterable[str]) -> list[str]: ...


class ResourceCatalogStore(Protocol):
    """Best-effort disposable catalog maintenance for blob records."""

    def try_update_index(self) -> str | None: ...


class ResourceStore(BlobStore, ResourceCatalogStore, Protocol):
    """Complete resource persistence composed from blob and catalog ports."""
