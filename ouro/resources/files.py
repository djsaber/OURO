"""Validated file publication shared by resource and Session persistence adapters."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile

from ouro.core.errors import ResourceIntegrityError, UnsafeResourcePathError
from ouro.core.resources import ResourceRecord
from ouro.local_io import replace_path_with_retry


def session_resource_directory(root: Path, session_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id) is None:
        raise UnsafeResourcePathError("invalid Session identity")
    return root / "sessions" / session_id / "resources" / "files"


def checked_file(directory: Path, record: ResourceRecord) -> Path:
    ResourceRecord.model_validate(record.model_dump())
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
) -> None:
    """Publish bytes before canonical references; failures may leave unreferenced files."""
    for record, content in items:
        if (
            len(content) != record.size
            or f"sha256:{hashlib.sha256(content).hexdigest()}" != record.digest
        ):
            raise ResourceIntegrityError(f"resource integrity mismatch: {record.resource_id}")
        path = checked_file(directory, record)
        directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise ResourceIntegrityError(f"resource file collision: {record.filename}")
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


def verify_resource_files(directory: Path, records: Iterable[ResourceRecord]) -> None:
    """Require a complete, unambiguous file closure before publishing Session metadata."""
    names: set[str] = set()
    identities: set[str] = set()
    for record in records:
        if record.filename.casefold() in names or record.resource_id in identities:
            raise ResourceIntegrityError("duplicate Session resource name or identity")
        names.add(record.filename.casefold())
        identities.add(record.resource_id)
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
