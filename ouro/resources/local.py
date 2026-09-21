from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import time
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

from ouro.core.base import new_id
from ouro.core.errors import (
    PersistenceError,
    ResourceIntegrityError,
    ResourceNotFoundError,
    UnsafeResourcePathError,
)
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.resources import (
    ResourceRecord,
    ResourceSource,
)
from ouro.local_io import read_text_atomic, write_json_atomic, write_model_atomic
from ouro.local_locks import FileOwnerLock

_RESOURCE_ID = re.compile(r"res_[0-9a-f]{64}\Z")
_CHUNK_SIZE = 1024 * 1024
_STALE_STAGING_SECONDS = 24 * 60 * 60


class LocalResourceStore:
    """Local immutable content-addressed blob and metadata store."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.resources_dir = self.root / "resources"
        self.records_dir = self.resources_dir / "records"
        self.blobs_dir = self.resources_dir / "blobs" / "sha256"
        self.staging_dir = self.resources_dir / "staging"
        self.index_path = self.resources_dir / "index.json"
        self.index_dirty_path = self.resources_dir / "index.dirty"
        self._lock_path = self.root / "locks" / "resources" / "catalog.lock"

    def initialize(self) -> None:
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        stale_before = time.time() - _STALE_STAGING_SECONDS
        for path in self.staging_dir.iterdir():
            if _is_stale_staging_file(path, stale_before=stale_before):
                with suppress(OSError):
                    path.unlink()
        if not self.index_path.exists():
            self.rebuild_index()

    def import_file(
        self,
        source_path: Path,
        *,
        source: ResourceSource,
        media_type: str,
        max_bytes: int,
    ) -> tuple[ResourceRecord, bool]:
        source_path = self._safe_import_path(source_path)
        staged_path = self.staging_dir / f"{new_id('resource')}.blob"
        try:
            digest, size = self._stage_file(source_path, staged_path, max_bytes=max_bytes)
            resource_id = f"res_{digest}"
            record = ResourceRecord(
                resource_id=resource_id,
                digest=f"sha256:{digest}",
                media_type=media_type,
                size=size,
                source=source,
            )
            with self._catalog_lock():
                existing = self._load_optional_record(resource_id)
                created = existing is None
                if existing is not None:
                    self._verify_record_compatibility(existing, record)
                    self._verify_blob(existing)
                    record = existing
                else:
                    self._publish_blob_and_record(staged_path, record)
                self._try_update_index_or_mark_dirty()
            return record, created
        finally:
            with suppress(OSError):
                staged_path.unlink()

    def load_record(self, resource_id: str, *, verify: bool = False) -> ResourceRecord:
        resource_id = _validated_resource_id(resource_id)
        path = self.records_dir / f"{resource_id}.json"
        try:
            record = decode_versioned_record_json(
                ResourceRecord,
                read_text_atomic(path),
                label=f"resource {resource_id}",
            )
        except FileNotFoundError as exc:
            raise ResourceNotFoundError(f"resource not found: {resource_id}") from exc
        except (OSError, ValueError) as exc:
            raise PersistenceError(f"could not load resource {resource_id}: {exc}") from exc
        if verify:
            self._verify_blob(record)
        return record

    def import_content(self, record: ResourceRecord, content: bytes) -> None:
        """Publish verified portable content while preserving canonical metadata."""
        self.import_contents(((record, content),))

    def import_contents(self, items: Iterable[tuple[ResourceRecord, bytes]]) -> None:
        # Blobs can remain unreferenced after a failed batch; the catalog must
        # still reflect those already published, without rescanning per blob.
        with self._catalog_lock():
            changed = False
            try:
                for record, content in items:
                    if (
                        len(content) != record.size
                        or f"sha256:{hashlib.sha256(content).hexdigest()}" != record.digest
                    ):
                        raise ResourceIntegrityError(
                            f"resource integrity mismatch: {record.resource_id}"
                        )
                    existing = self._load_optional_record(record.resource_id)
                    if existing is not None:
                        self._verify_record_compatibility(existing, record)
                        self._verify_blob(existing)
                        continue
                    staged = self.staging_dir / f"{new_id('resource')}.blob"
                    try:
                        with staged.open("xb") as stream:
                            stream.write(content)
                            stream.flush()
                            os.fsync(stream.fileno())
                        self._publish_blob_and_record(staged, record)
                        changed = True
                    finally:
                        staged.unlink(missing_ok=True)
            finally:
                if changed:
                    self._try_update_index_or_mark_dirty()

    def load_content(
        self,
        resource_id: str,
        *,
        max_bytes: int | None = None,
    ) -> tuple[ResourceRecord, bytes]:
        """Read and verify immutable bytes without exposing the backing path."""

        record = self.load_record(resource_id)
        if max_bytes is not None and record.size > max_bytes:
            raise ValueError(
                f"resource {resource_id} exceeds model attachment limit of {max_bytes} bytes"
            )
        blob_path = self._blob_path(record)
        try:
            content = blob_path.read_bytes()
        except FileNotFoundError as exc:
            raise ResourceIntegrityError(f"resource blob is missing: {resource_id}") from exc
        except OSError as exc:
            raise PersistenceError(f"could not read resource {resource_id}: {exc}") from exc
        actual_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if actual_digest != record.digest or len(content) != record.size:
            raise ResourceIntegrityError(f"resource integrity mismatch: {resource_id}")
        return record, content

    def export_file(self, resource_id: str, destination: Path, *, overwrite: bool) -> Path:
        record = self.load_record(resource_id, verify=True)
        destination = self._safe_export_path(destination)
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        blob_path = self._blob_path(record)
        temp_path: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=destination.parent, delete=False) as target:
                temp_path = Path(target.name)
                with blob_path.open("rb") as source_file:
                    shutil.copyfileobj(source_file, target, length=_CHUNK_SIZE)
                target.flush()
                os.fsync(target.fileno())
            temp_path.replace(destination)
            return destination
        except OSError as exc:
            raise PersistenceError(f"could not export resource {resource_id}: {exc}") from exc
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink()

    def delete_resources(self, resource_ids: Iterable[str]) -> list[str]:
        """Delete candidate records after the application proves they are unreachable."""

        with self._catalog_lock():
            removed: list[str] = []
            for resource_id in dict.fromkeys(resource_ids):
                resource_id = _validated_resource_id(resource_id)
                record = self._load_optional_record(resource_id)
                if record is None:
                    continue
                with suppress(FileNotFoundError):
                    (self.records_dir / f"{resource_id}.json").unlink()
                digest = record.digest.removeprefix("sha256:")
                if not any(
                    candidate.digest == record.digest
                    for candidate in self._scan_records()
                    if candidate.resource_id != resource_id
                ):
                    with suppress(FileNotFoundError):
                        (self.blobs_dir / digest).unlink()
                removed.append(resource_id)
            if removed:
                self._try_update_index_or_mark_dirty()
            return removed

    def rebuild_index(self) -> None:
        with self._catalog_lock():
            self._rebuild_index_locked()

    def _rebuild_index_locked(self) -> None:
        records = self._scan_records()
        write_json_atomic(
            self.index_path,
            {"resources": [record.model_dump(mode="json") for record in records]},
        )
        with suppress(OSError):
            self.index_dirty_path.unlink()

    def try_update_index(self) -> str | None:
        try:
            self.rebuild_index()
        except (OSError, PersistenceError, ResourceNotFoundError, ValueError) as exc:
            self._mark_index_dirty(str(exc))
            return str(exc)
        return None

    @staticmethod
    def infer_media_type(path: Path) -> str:
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    def _safe_import_path(self, path: Path) -> Path:
        try:
            if _path_contains_symlink(path):
                raise UnsafeResourcePathError(f"symbolic links cannot be imported: {path}")
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise UnsafeResourcePathError(f"unsafe resource path {path}: {exc}") from exc
        if not resolved.is_file():
            raise UnsafeResourcePathError(f"resource source is not a regular file: {path}")
        return resolved

    def _safe_export_path(self, path: Path) -> Path:
        if _path_contains_symlink(path):
            raise UnsafeResourcePathError(f"cannot export through symbolic link: {path}")
        parent = path.parent.resolve(strict=False)
        return parent / path.name

    def _stage_file(self, source: Path, target: Path, *, max_bytes: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as input_file, target.open("xb") as output_file:
                while chunk := input_file.read(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"resource exceeds maximum size of {max_bytes} bytes")
                    digest.update(chunk)
                    output_file.write(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
        except OSError as exc:
            raise PersistenceError(f"could not stage resource {source.name}: {exc}") from exc
        return digest.hexdigest(), size

    def _publish_blob_and_record(self, staged_path: Path, record: ResourceRecord) -> bool:
        blob_path = self._blob_path(record)
        published_blob = False
        try:
            if not blob_path.exists():
                staged_path.replace(blob_path)
                published_blob = True
            else:
                self._verify_blob(record)
            write_model_atomic(self.records_dir / f"{record.resource_id}.json", record)
        except (OSError, PersistenceError) as exc:
            if published_blob:
                with suppress(OSError):
                    blob_path.unlink()
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(
                f"could not publish resource {record.resource_id}: {exc}"
            ) from exc
        return published_blob

    def _verify_blob(self, record: ResourceRecord) -> None:
        blob_path = self._blob_path(record)
        try:
            digest = hashlib.sha256()
            size = 0
            with blob_path.open("rb") as file:
                while chunk := file.read(_CHUNK_SIZE):
                    size += len(chunk)
                    digest.update(chunk)
        except FileNotFoundError as exc:
            raise ResourceIntegrityError(f"resource blob is missing: {record.resource_id}") from exc
        except OSError as exc:
            raise PersistenceError(
                f"could not verify resource {record.resource_id}: {exc}"
            ) from exc
        actual = f"sha256:{digest.hexdigest()}"
        if actual != record.digest or size != record.size:
            raise ResourceIntegrityError(f"resource integrity mismatch: {record.resource_id}")

    @staticmethod
    def _verify_record_compatibility(existing: ResourceRecord, incoming: ResourceRecord) -> None:
        if existing.digest != incoming.digest or existing.size != incoming.size:
            raise ResourceIntegrityError(f"resource identity collision: {incoming.resource_id}")

    def _scan_records(self) -> list[ResourceRecord]:
        records: list[ResourceRecord] = []
        for path in sorted(self.records_dir.glob("res_*.json")):
            try:
                records.append(
                    decode_versioned_record_json(
                        ResourceRecord,
                        read_text_atomic(path),
                        label=f"resource record {path}",
                    )
                )
            except (OSError, ValueError) as exc:
                raise PersistenceError(f"could not scan resource record {path}: {exc}") from exc
        return records

    def _load_optional_record(self, resource_id: str) -> ResourceRecord | None:
        try:
            return self.load_record(resource_id)
        except ResourceNotFoundError:
            return None

    def _blob_path(self, record: ResourceRecord) -> Path:
        return self.blobs_dir / record.digest.removeprefix("sha256:")

    def _catalog_lock(self) -> FileOwnerLock:
        for attempt in range(101):
            lock = FileOwnerLock(
                self._lock_path,
                owner={"scope": "resource_catalog", "operation_id": new_id("resource")},
            )
            if lock.acquire():
                return lock
            if attempt < 100:
                time.sleep(0.01)
        raise PersistenceError("resource catalog remained busy for one second")

    def _try_update_index_or_mark_dirty(self) -> None:
        try:
            self._rebuild_index_locked()
        except (OSError, PersistenceError, ResourceNotFoundError, ValueError) as exc:
            self._mark_index_dirty(str(exc))

    def _mark_index_dirty(self, error: str) -> None:
        try:
            write_json_atomic(self.index_dirty_path, {"error": error})
        except PersistenceError:
            pass


def _validated_resource_id(resource_id: str) -> str:
    if not _RESOURCE_ID.fullmatch(resource_id):
        raise ResourceNotFoundError(f"invalid resource id: {resource_id!r}")
    return resource_id


def _path_contains_symlink(path: Path) -> bool:
    absolute = path.absolute()
    return any(
        candidate.is_symlink() or candidate.is_junction()
        for candidate in (absolute, *absolute.parents)
    )


def _is_stale_staging_file(path: Path, *, stale_before: float) -> bool:
    try:
        return path.is_file() and path.stat().st_mtime < stale_before
    except OSError:
        return False
