"""Durable resource manifests before a Session accepts its first input."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ouro.core.errors import PersistenceError, ResourceIntegrityError
from ouro.core.resources import SessionResourceReference, SessionResourceReferences
from ouro.local_io import read_text_atomic, write_model_atomic
from ouro.resources.files import checked_file, session_resource_directory


def prepared_manifest(root: Path, identity: str) -> Path:
    directory = session_resource_directory(root, identity)
    path = directory.parent / "prepared.json"
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.absolute().parents)):
        raise PersistenceError("Prepared resource path contains a symbolic link")
    return path


def load_prepared_resources(root: Path, identity: str) -> list[SessionResourceReference] | None:
    path = prepared_manifest(root, identity)
    try:
        return SessionResourceReferences.model_validate_json(read_text_atomic(path)).resources
    except FileNotFoundError:
        return None
    except (OSError, ValidationError) as exc:
        raise PersistenceError(f"Could not read prepared resources {identity}: {exc}") from exc


def save_prepared_resources(
    root: Path, identity: str, records: list[SessionResourceReference]
) -> None:
    write_model_atomic(
        prepared_manifest(root, identity), SessionResourceReferences(resources=records)
    )


def validate_prepared_resources(
    root: Path, identity: str, records: list[SessionResourceReference]
) -> None:
    """Adoption checks identity and availability; content is read by normal input preparation."""
    records_before_adoption = load_prepared_resources(root, identity)
    if records_before_adoption is None:
        raise ResourceIntegrityError("Resource namespace is not reserved")
    prepared = {item.resource_id: item for item in records_before_adoption}
    directory = session_resource_directory(root, identity)
    for record in records:
        if prepared.get(record.resource_id) != record:
            raise ResourceIntegrityError("Resource does not belong to the prepared input")
        try:
            if checked_file(directory, record).stat().st_size != record.size:
                raise ResourceIntegrityError(f"Prepared resource size changed: {record.filename}")
        except OSError as exc:
            raise ResourceIntegrityError(
                f"Prepared resource is unavailable: {record.filename}"
            ) from exc
