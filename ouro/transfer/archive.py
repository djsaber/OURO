from __future__ import annotations

import json
import os
import re
import stat
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any, Literal, get_args
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from pydantic import ConfigDict, ValidationError, field_validator

from ouro.core.base import OuroModel
from ouro.core.errors import OuroError

DataKind = Literal["providers", "provider-templates", "mcp", "agents", "sessions", "skills"]
DATA_KINDS: tuple[DataKind, ...] = get_args(DataKind)
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000
_RESERVED_NAME = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", re.I)


class DataTransferError(OuroError):
    """An archive or target cannot satisfy the data transfer contract."""


class ArchiveManifest(OuroModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    format: Literal["ouro-data"] = "ouro-data"
    version: Literal[1] = 1
    kind: DataKind
    data: dict[str, Any]

    @field_validator("version", mode="before")
    @classmethod
    def require_current_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported archive version")
        return value


@dataclass(frozen=True)
class DataArchive:
    manifest: ArchiveManifest
    files: dict[str, bytes] = field(default_factory=dict)


def portable_path(value: str) -> PurePosixPath:
    """Accept only unambiguous relative paths on both Windows and POSIX."""
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        not value
        or path.is_absolute()
        or any(character in value for character in '\\:\x00<>"|?*')
        or any(ord(character) < 32 for character in value)
        or any(
            part in {"", ".", ".."} or part.endswith((" ", ".")) or _RESERVED_NAME.fullmatch(part)
            for part in parts
        )
    ):
        raise DataTransferError("archive contains an unsafe path")
    return path


def read_archive(path: Path, kind: DataKind | None = None) -> DataArchive:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if (
                len(entries) > MAX_ARCHIVE_FILES
                or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES
            ):
                raise DataTransferError("archive exceeds the file count or size limit")
            seen: set[str] = set()
            files: dict[str, bytes] = {}
            for entry in entries:
                portable_path(entry.filename)
                key = entry.filename.casefold()
                mode = entry.external_attr >> 16
                if (
                    key in seen
                    or entry.is_dir()
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    or entry.flag_bits & 1
                ):
                    raise DataTransferError("archive contains duplicate or unsupported entries")
                seen.add(key)
                files[entry.filename] = archive.read(entry)
        manifest_bytes = files.pop("manifest.json", None)
        if manifest_bytes is None:
            raise DataTransferError("archive is missing manifest.json")
        manifest = ArchiveManifest.model_validate_json(manifest_bytes)
        if manifest.model_fields_set != {"format", "version", "kind", "data"}:
            raise DataTransferError("archive manifest must declare its format and version")
        if kind is not None and manifest.kind != kind:
            raise DataTransferError(f"archive kind is {manifest.kind}; requested {kind}")
        return DataArchive(manifest, files)
    except (
        OSError,
        BadZipFile,
        RuntimeError,
        ValidationError,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise DataTransferError("could not read a valid OURO data archive") from exc


def write_archive(path: Path, data: DataArchive, *, replace: bool) -> None:
    manifest = json.dumps(data.manifest.model_dump(mode="json"), ensure_ascii=False).encode()
    entries = {"manifest.json": manifest, **data.files}
    if "manifest.json" in data.files or len(entries) > MAX_ARCHIVE_FILES:
        raise DataTransferError("archive contains too many files or a reserved entry")
    if sum(map(len, entries.values())) > MAX_ARCHIVE_BYTES:
        raise DataTransferError("archive exceeds the size limit")
    names: set[str] = set()
    for name in entries:
        portable_path(name)
        if name.casefold() in names:
            raise DataTransferError("archive contains case-insensitive path collisions")
        names.add(name.casefold())
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in sorted(entries.items()):
                archive.writestr(name, content)
        if replace:
            temporary.replace(path)
        elif os.name == "nt":
            temporary.rename(path)
        else:
            # Exclusive publication also protects against a destination created
            # after validation, without exposing an incomplete archive.
            os.link(temporary, path)
    except FileExistsError as exc:
        raise DataTransferError("destination exists; use --replace to overwrite it") from exc
    except OSError as exc:
        raise DataTransferError(f"could not write archive: {path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
