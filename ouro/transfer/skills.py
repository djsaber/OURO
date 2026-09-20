from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ConfigDict, ValidationError

from ouro.config.file import JsonConfigFile
from ouro.config.skills import SkillConfigManager
from ouro.core.base import OuroModel
from ouro.skills.loader import SkillLoader
from ouro.skills.parser import SkillDocumentParser
from ouro.transfer.archive import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_FILES,
    ArchiveManifest,
    DataArchive,
    DataTransferError,
    portable_path,
)

_SKIP_DIRECTORIES = {".git", ".hg", ".svn", "__pycache__", ".venv", "node_modules"}


class SkillArchiveData(OuroModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    names: tuple[str, ...]
    executable_files: tuple[str, ...] = ()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def export_skills(home: Path, identities: tuple[str, ...]) -> DataArchive:
    with JsonConfigFile(home).mutation():
        return _export_skills(home, identities)


def _export_skills(home: Path, identities: tuple[str, ...]) -> DataArchive:
    root = home / "skills"
    if _is_link(root):
        raise DataTransferError("native skill root cannot be a symbolic link or junction")
    catalog = SkillLoader(root).load_catalog()
    if any(
        item.level == "error"
        or item.code in {"duplicate_name", "scan_failed", "scan_limit_reached"}
        for item in catalog.diagnostics
    ):
        raise DataTransferError("fix invalid or incomplete native skill discovery before exporting")
    selected = [item for item in catalog.skills if not identities or item.name in identities]
    missing = set(identities) - {item.name for item in selected}
    if missing:
        raise DataTransferError(f"native skills not found: {', '.join(sorted(missing))}")
    if len({item.name for item in selected}) != len(selected):
        raise DataTransferError("duplicate native skill names")
    files: dict[str, bytes] = {}
    executables: list[str] = []
    size = 0
    for skill in selected:
        directory = Path(skill.location).parent
        if not directory.resolve().is_relative_to(root.resolve()):
            raise DataTransferError("skill source escapes the native skills directory")
        for current, dirs, names in os.walk(directory, followlinks=False):
            parent = Path(current)
            if _is_link(parent) or any(_is_link(parent / name) for name in [*dirs, *names]):
                raise DataTransferError("skill export does not follow symbolic links")
            dirs[:] = [name for name in dirs if name not in _SKIP_DIRECTORIES]
            for name in sorted(names):
                path = parent / name
                archive_path = f"skills/{skill.name}/{path.relative_to(directory).as_posix()}"
                portable_path(archive_path)
                size += path.stat().st_size
                if len(files) + 2 > MAX_ARCHIVE_FILES or size > MAX_ARCHIVE_BYTES:
                    raise DataTransferError("skills exceed the archive limits")
                files[archive_path] = path.read_bytes()
                if path.stat().st_mode & 0o111:
                    executables.append(archive_path)
    data = SkillArchiveData(
        names=tuple(item.name for item in selected), executable_files=tuple(executables)
    )
    return DataArchive(ArchiveManifest(kind="skills", data=data.model_dump(mode="json")), files)


def import_skills(home: Path, archive: DataArchive, *, replace: bool) -> int:
    try:
        data = SkillArchiveData.model_validate(archive.manifest.data)
    except ValidationError as exc:
        raise DataTransferError("invalid skill archive manifest") from exc
    names = data.names
    if len({name.casefold() for name in names}) != len(names) or not set(
        data.executable_files
    ).issubset(archive.files):
        raise DataTransferError("invalid skill archive manifest")
    for name in names:
        if len(portable_path(name).parts) != 1:
            raise DataTransferError("invalid skill name")
    root = home / "skills"
    if _is_link(root):
        raise DataTransferError("native skill root cannot be a symbolic link or junction")
    root.mkdir(parents=True, exist_ok=True)
    temporary = TemporaryDirectory(prefix=".skill-import-", dir=home, delete=False)
    staging = Path(temporary.name)
    retain_backups = False
    try:
        for name, content in archive.files.items():
            parts = portable_path(name).parts
            if len(parts) < 3 or parts[0] != "skills" or parts[1] not in names:
                raise DataTransferError("skill archive contains undeclared files")
            target = staging.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if name in data.executable_files:
                target.chmod(target.stat().st_mode | 0o111)
        for name in names:
            parsed = SkillDocumentParser().parse(staging / "skills" / name / "SKILL.md")
            if parsed.skill is None or parsed.skill.metadata.name != name:
                raise DataTransferError(f"invalid skill document: {name}")
        # Serialize publication with native installation and policy changes.
        with JsonConfigFile(home).mutation():
            policy = SkillConfigManager(home)
            policy.snapshot()
            backups: list[str] = []
            installed: list[str] = []
            for name in names:
                target = root / name
                if _is_link(target) or (target.exists() and not target.is_dir()):
                    raise DataTransferError(f"unsafe skill destination: {name}")
                if target.exists() and not replace:
                    raise DataTransferError(f"skill exists: {name}; use --replace")
            try:
                for name in names:
                    target = root / name
                    if target.exists():
                        backup = staging / "backups" / name
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        target.rename(backup)
                        backups.append(name)
                    (staging / "skills" / name).rename(target)
                    installed.append(name)
                policy.forget_installs(names)
            except BaseException:
                retain_backups = True
                try:
                    for name in reversed(installed):
                        (root / name).rename(staging / "skills" / name)
                    for name in reversed(backups):
                        (staging / "backups" / name).rename(root / name)
                except OSError as exc:
                    raise DataTransferError(
                        f"skill import rollback failed; original backups retained at {staging}"
                    ) from exc
                retain_backups = False
                raise
    finally:
        if not retain_backups:
            temporary.cleanup()
    return len(names)
