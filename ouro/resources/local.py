from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile

from ouro.core.base import new_id
from ouro.core.errors import (
    PersistenceError,
    ResourceIntegrityError,
    UnsafeResourcePathError,
    UnsupportedMediaTypeError,
)
from ouro.core.resources import ResourceRecord, ResourceSource, SessionResourceReference
from ouro.local_io import replace_path_with_retry
from ouro.local_locks import FileMutationLock
from ouro.resources.files import (
    checked_file,
    session_resource_directory,
    verify_resource_files,
    write_resource_contents,
)
from ouro.resources.prepared import (
    load_prepared_resources,
    prepared_manifest,
    save_prepared_resources,
)

_CHUNK_SIZE = 1024 * 1024


class LocalResourceStore:
    """Session-local named files, without a global catalog or metadata authority."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).absolute()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def reserve(self, identity: str | None = None) -> str:
        identity = identity or new_id("ses")
        if not prepared_manifest(self.root, identity).exists():
            save_prepared_resources(self.root, identity, [])
        return identity

    def load_prepared(self, identity: str) -> list[SessionResourceReference] | None:
        return load_prepared_resources(self.root, identity)

    def save_prepared(self, identity: str, records: list[SessionResourceReference]) -> None:
        save_prepared_resources(self.root, identity, records)

    def discard_prepared(self, identity: str) -> None:
        path = prepared_manifest(self.root, identity)
        if path.exists():
            # The service holds the Session mutation lock and has checked non-adoption.
            shutil.rmtree(path.parent.parent)

    @staticmethod
    def infer_media_type(path: Path) -> str:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def directory_for(self, session_id: str) -> Path:
        return session_resource_directory(self.root, session_id)

    def path_for(self, session_id: str, record: ResourceRecord) -> Path:
        return checked_file(session_resource_directory(self.root, session_id), record)

    def import_file(
        self,
        session_id: str,
        source_path: Path,
        *,
        source: ResourceSource,
        media_type: str,
        max_bytes: int,
    ) -> ResourceRecord:
        if re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type) is None:
            raise UnsupportedMediaTypeError("resource media_type must use type/subtype syntax")
        source_path = self._safe_import_path(source_path)
        directory = session_resource_directory(self.root, session_id)
        lock = FileMutationLock(self.root / "locks" / "resources" / f"{session_id}.lock")
        if not lock.acquire(timeout=5):
            raise PersistenceError("Session resource directory remained busy")
        try:
            if _path_contains_symlink(directory):
                raise UnsafeResourcePathError("resource directory contains a symbolic link")
            directory.mkdir(parents=True, exist_ok=True)
            filename = _available_name(directory, source.name)
            staging = directory.parent / ".staging"
            if _path_contains_symlink(staging):
                raise UnsafeResourcePathError("resource staging contains a symbolic link")
            staging.mkdir(exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with NamedTemporaryFile(dir=staging, delete=False) as target:
                temporary = Path(target.name)
                try:
                    with source_path.open("rb") as original:
                        while chunk := original.read(_CHUNK_SIZE):
                            size += len(chunk)
                            if size > max_bytes:
                                raise ValueError(
                                    f"resource exceeds maximum size of {max_bytes} bytes"
                                )
                            digest.update(chunk)
                            target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                except BaseException:
                    target.close()
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                record = ResourceRecord(
                    filename=filename,
                    digest=f"sha256:{digest.hexdigest()}",
                    size=size,
                    media_type=media_type,
                    source=source,
                )
                replace_path_with_retry(temporary, self.path_for(session_id, record))
                return record
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            lock.release()

    def import_contents(
        self, session_id: str, items: Iterable[tuple[ResourceRecord, bytes]]
    ) -> None:
        write_resource_contents(session_resource_directory(self.root, session_id), items)

    def load_content(
        self, session_id: str, record: ResourceRecord, *, max_bytes: int | None = None
    ) -> bytes:
        if max_bytes is not None and record.size > max_bytes:
            raise ValueError(f"resource exceeds read limit of {max_bytes} bytes")
        return self.read_ranges(session_id, record, ((0, record.size),))[0]

    def verify(self, session_id: str, record: ResourceRecord) -> None:
        verify_resource_files(session_resource_directory(self.root, session_id), (record,))

    def read_ranges(
        self, session_id: str, record: ResourceRecord, ranges: tuple[tuple[int, int], ...]
    ) -> tuple[bytes, ...]:
        if any(start < 0 or length < 0 or start + length > record.size for start, length in ranges):
            raise ValueError("resource ranges must lie within the recorded size")
        parts = []
        try:
            with self.path_for(session_id, record).open("rb", buffering=0) as stream:
                if os.fstat(stream.fileno()).st_size != record.size:
                    raise ResourceIntegrityError(f"resource size mismatch: {record.filename}")
                for start, length in ranges:
                    stream.seek(start)
                    part = stream.read(length)
                    if len(part) != length:
                        raise ResourceIntegrityError(
                            f"resource range unavailable: {record.filename}"
                        )
                    parts.append(part)
                if os.fstat(stream.fileno()).st_size != record.size:
                    raise ResourceIntegrityError(f"resource size changed: {record.filename}")
        except FileNotFoundError as exc:
            raise ResourceIntegrityError(f"resource file is missing: {record.filename}") from exc
        except OSError as exc:
            raise PersistenceError(f"could not read resource {record.filename}: {exc}") from exc
        return tuple(parts)

    def export_file(
        self, session_id: str, record: ResourceRecord, destination: Path, *, overwrite: bool
    ) -> Path:
        if _path_contains_symlink(destination):
            raise UnsafeResourcePathError("cannot export through symbolic links")
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(dir=destination.parent, delete=False) as target:
                temporary = Path(target.name)
                digest, size = hashlib.sha256(), 0
                with self.path_for(session_id, record).open("rb") as source:
                    while chunk := source.read(_CHUNK_SIZE):
                        digest.update(chunk)
                        size += len(chunk)
                        target.write(chunk)
                if size != record.size or f"sha256:{digest.hexdigest()}" != record.digest:
                    raise ResourceIntegrityError(f"resource integrity mismatch: {record.filename}")
                target.flush()
                os.fsync(target.fileno())
            replace_path_with_retry(temporary, destination)
            return destination
        except FileNotFoundError as exc:
            raise ResourceIntegrityError(f"resource file unavailable: {record.filename}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete_resources(self, session_id: str, records: Iterable[ResourceRecord]) -> list[str]:
        removed = []
        for record in records:
            if record.resource_id in removed:
                continue
            self.path_for(session_id, record).unlink(missing_ok=True)
            removed.append(record.resource_id)
        return removed

    @staticmethod
    def _safe_import_path(path: Path) -> Path:
        if _path_contains_symlink(path):
            raise UnsafeResourcePathError(f"symbolic links cannot be imported: {path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise UnsafeResourcePathError(f"resource source is not a regular file: {path}")
        return resolved


def _available_name(directory: Path, name: str) -> str:
    safe = (
        re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unicodedata.normalize("NFC", name)).rstrip(" .")
        or "file"
    )
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", safe):
        safe = "_" + safe
    original_suffix = Path(safe).suffix
    suffix = original_suffix[:24]
    stem = safe[: -len(original_suffix)] if original_suffix else safe
    stem = stem.encode("utf-8")[:160].decode("utf-8", errors="ignore")
    existing = {path.name.casefold() for path in directory.iterdir()}
    candidate = stem + suffix
    index = 2
    while candidate.casefold() in existing:
        candidate = f"{stem} ({index}){suffix}"
        index += 1
    return candidate


def _path_contains_symlink(path: Path) -> bool:
    absolute = path.absolute()
    return any(
        candidate.is_symlink() or candidate.is_junction()
        for candidate in (absolute, *absolute.parents)
    )
