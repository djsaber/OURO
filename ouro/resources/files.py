"""Validated file publication shared by resource and Session persistence adapters."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile

from ouro.core.errors import (
    PersistenceError,
    ResourceIntegrityError,
    UnsafeResourcePathError,
    UnsupportedMediaTypeError,
)
from ouro.core.resources import ResourceRecord, ResourceSource
from ouro.local_io import replace_path_with_retry
from ouro.local_locks import FileMutationLock


def create_resource(
    root: Path,
    session_id: str,
    chunks: Iterable[bytes],
    *,
    source: ResourceSource,
    media_type: str,
    max_bytes: int | None = None,
    internal: bool = False,
) -> ResourceRecord:
    """Publish a new named resource from file chunks or already observed bytes."""
    if re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type) is None:
        raise UnsupportedMediaTypeError("resource media_type must use type/subtype syntax")
    directory = session_resource_directory(root, session_id)
    lock = FileMutationLock(root / "locks" / "resources" / f"{session_id}.lock")
    if not lock.acquire(timeout=5):
        raise PersistenceError("Session resource directory remained busy")
    temporary = None
    try:
        staging = directory.parent / ".staging"
        for path in (directory, staging):
            if any(p.is_symlink() or p.is_junction() for p in (path, *path.absolute().parents)):
                raise UnsafeResourcePathError("resource directory contains a symbolic link")
        directory.mkdir(parents=True, exist_ok=True)
        staging.mkdir(exist_ok=True)
        filename = _available_name(directory, source.name)
        digest, size = hashlib.sha256(), 0
        with NamedTemporaryFile(dir=staging, delete=False) as target:
            temporary = Path(target.name)
            for chunk in chunks:
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise ValueError(f"resource exceeds maximum size of {max_bytes} bytes")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        record = ResourceRecord(
            filename=filename,
            digest=f"sha256:{digest.hexdigest()}",
            size=size,
            media_type=media_type,
            source=source,
            internal=internal,
        )
        replace_path_with_retry(temporary, checked_file(directory, record))
        return record
    finally:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        finally:
            lock.release()


def _available_name(directory: Path, name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .") or "file"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", safe):
        safe = "_" + safe
    suffix = Path(safe).suffix
    stem = safe[: -len(suffix)] if suffix else safe
    with os.scandir(directory) as entries:
        existing = {entry.name.casefold() for entry in entries}
    candidate = safe
    index = 2
    while candidate.casefold() in existing:
        candidate = f"{stem} ({index}){suffix}"
        index += 1
    return candidate


def read_resource_content(directory: Path, record: ResourceRecord) -> bytes:
    """Read one selected immutable resource; publication owns its digest check."""
    try:
        content = checked_file(directory, record).read_bytes()
    except FileNotFoundError as exc:
        raise ResourceIntegrityError(f"resource file is missing: {record.resource_id}") from exc
    if len(content) != record.size:
        raise ResourceIntegrityError(f"resource size mismatch: {record.resource_id}")
    return content


def require_published_resource(directory: Path, record: ResourceRecord) -> None:
    try:
        if checked_file(directory, record).stat().st_size == record.size:
            return
    except FileNotFoundError:
        pass
    raise ResourceIntegrityError(f"resource is not published: {record.resource_id}")


def session_resource_directory(root: Path, session_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id) is None:
        raise UnsafeResourcePathError("invalid Session identity")
    return root / "sessions" / session_id / "resources" / "files"


def checked_file(directory: Path, record: ResourceRecord) -> Path:
    path = directory / record.filename
    if any(
        candidate.is_symlink() or candidate.is_junction()
        for candidate in (path, *path.absolute().parents)
    ):
        raise UnsafeResourcePathError(f"symbolic links are not allowed in resource paths: {path}")
    return path


def write_resource_contents(
    directory: Path,
    items: Iterable[tuple[ResourceRecord, bytes]],
    *,
    references: Iterable[ResourceRecord] = (),
) -> None:
    """Publish bytes and verify the reference closure before metadata is committed."""
    published: set[tuple[str, int, str]] = set()
    for record, content in items:
        if not record.matches_content(content):
            raise ResourceIntegrityError(f"resource integrity mismatch: {record.resource_id}")
        path = checked_file(directory, record)
        directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise ResourceIntegrityError(f"resource file collision: {record.filename}")
            published.add((record.filename, record.size, record.digest))
            continue
        with NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            replace_path_with_retry(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        published.add((record.filename, record.size, record.digest))

    names: set[str] = set()
    identities: set[str] = set()
    for record in references:
        if record.filename.casefold() in names or record.resource_id in identities:
            raise ResourceIntegrityError("duplicate Session resource name or identity")
        names.add(record.filename.casefold())
        identities.add(record.resource_id)
        if (record.filename, record.size, record.digest) in published:
            continue
        path = checked_file(directory, record)
        digest, size = hashlib.sha256(), 0
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise ResourceIntegrityError(f"resource file unavailable: {path}") from exc
        if size != record.size or f"sha256:{digest.hexdigest()}" != record.digest:
            raise ResourceIntegrityError(f"resource integrity mismatch: {path}")


def verify_resource_files(directory: Path, records: Iterable[ResourceRecord]) -> None:
    """Verify existing files without publishing new content."""
    write_resource_contents(directory, (), references=records)
