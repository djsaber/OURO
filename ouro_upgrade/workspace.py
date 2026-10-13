"""Stage and validate first; publish a recoverable set of file replacements second."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ouro.auth.file_permissions import reject_link, restrict_to_user
from ouro.local_io import replace_path_with_retry, write_json_atomic
from ouro.local_locks import FileAccessLock
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.progress import ProgressCallback, UpgradePhase, UpgradeProgress
from ouro_upgrade.records import loads


def digest(path: Path, *, sync: bool = False, directory: bool = False) -> str | None:
    if not path.exists():
        return None
    if directory:
        checksum = hashlib.sha256()
        for item in sorted(path.iterdir()):
            reject_link(item)
            is_directory = item.is_dir()
            value = digest(item, sync=sync, directory=is_directory)
            checksum.update(json.dumps([item.name, is_directory, value]).encode("utf-8"))
        if sync:
            sync_directory(path)
        return checksum.hexdigest()
    with path.open("rb+") if sync and os.name == "nt" else path.open("rb") as stream:
        file_checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if sync:
            os.fsync(stream.fileno())
        return file_checksum


def safe_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    if not isinstance(relative, str) or PurePosixPath(relative).as_posix() != relative:
        raise UpgradeError("invalid relative upgrade path")
    parts = PurePosixPath(relative).parts
    if not parts or any(
        part in {".", ".."} or ":" in part or "\\" in part or part.endswith((".", " "))
        for part in parts
    ):
        raise UpgradeError("invalid relative upgrade path")
    if PurePosixPath(relative).is_absolute() or parts[0].lower() == ".upgrade":
        raise UpgradeError("invalid business data path")
    path = root
    for part in parts:
        path /= part
        reject_link(path)
    if path.exists() and not (path.is_dir() if directory else path.is_file()):
        raise UpgradeError(f"unexpected path type: {relative}")
    return path


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def install(source: Path | None, target: Path, mode: int | None, *, consume: bool = False) -> None:
    if source is None:
        target.unlink(missing_ok=True)
    elif consume:
        # Staged files are already durable and share the home's filesystem. Only
        # rollback copies must survive installation for a repeatable restore.
        target.parent.mkdir(parents=True, exist_ok=True)
        restrict_to_user(source)
        if mode is not None and os.name != "nt":
            source.chmod(mode)
        replace_path_with_retry(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            restrict_to_user(temporary)
            with source.open("rb") as src, temporary.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if mode is not None and os.name != "nt":
                temporary.chmod(mode)
            replace_path_with_retry(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    sync_directory(target.parent)


def restore_directory(source: Path, target: Path, mode: int | None) -> None:
    # Both roots have been validated, and the complete backup was checked before
    # restore began. Keep the backup so an interrupted copy can be repeated.
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    if mode is not None and os.name != "nt":
        target.chmod(mode)
    digest(target, sync=True, directory=True)
    sync_directory(target.parent)


@dataclass(frozen=True)
class Change:
    path: str
    before: str | None
    after: str | None
    mode: int | None
    directory: bool = False


class Workspace:
    """Own one offline attempt and its immutable rollback materials."""

    def __init__(
        self, home: Path, *, progress: ProgressCallback | None = None, check_only: bool = False
    ) -> None:
        self.home = home
        self.root = home / ".upgrade"
        self.marker = self.root / "in-progress.json"
        self.identifier = uuid4().hex
        self.run = self.root / self.identifier
        self.lock = FileAccessLock(self.root / "lock", exclusive=True)
        self.observed: dict[str, str | None] = {}
        self.changes: dict[str, Change] = {}
        self.published = False
        self._progress = progress
        self._check_only = check_only
        self._reader = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ouro-upgrade-read")

    def progress(self, phase: UpgradePhase, completed: int = 0, total: int | None = None) -> None:
        if self._progress is not None:
            self._progress(UpgradeProgress(phase, completed, total))

    def __enter__(self) -> Workspace:
        for path in (*reversed(self.home.parents), self.home, self.root):
            reject_link(path)
        if not self.home.is_dir():
            raise UpgradeError("--home must name an existing ordinary directory")
        self.root.mkdir(exist_ok=True)
        restrict_to_user(self.root, directory=True)
        reject_link(self.lock.path)
        if not self.lock.acquire():
            raise UpgradeError("another data upgrade is using this home")
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self._reader.shutdown(wait=True, cancel_futures=True)
            if self.run.exists() and not self.published:
                shutil.rmtree(self.run)
        finally:
            self.lock.release()

    def start(self) -> None:
        reject_link(self.marker)
        if self.marker.exists():
            raise UpgradeError(
                "unfinished data maintenance; open the TUI recovery menu "
                "or run ouro-upgrade --home PATH --restore for an unfinished upgrade"
            )
        self.run.mkdir()
        (self.run / "new").mkdir()
        (self.run / "before").mkdir()
        (self.run / "scratch").mkdir()

    def path(self, relative: str) -> Path:
        return safe_path(self.home, relative)

    def source(self, relative: str) -> Path:
        path = self.path(relative)
        if relative not in self.observed:
            self.observed[relative] = digest(path)
        return path

    def read_bytes(self, relative: str) -> bytes:
        """Read a record and fingerprint the exact bytes being parsed in one pass."""
        return self._observe(relative, safe_path(self.home, relative).read_bytes())

    def read_many(self, relatives: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        """Prefetch independent files; observation and all mutations stay on the caller."""

        def read(relative: str) -> tuple[str, bytes]:
            try:
                return relative, safe_path(self.home, relative).read_bytes()
            except OSError as exc:
                raise UpgradeError(
                    f"{relative}: cannot read record ({type(exc).__name__})"
                ) from exc

        for relative, data in self._reader.map(read, relatives, buffersize=4):
            yield relative, self._observe(relative, data)

    def _observe(self, relative: str, data: bytes) -> bytes:
        if relative not in self.observed:
            self.observed[relative] = hashlib.sha256(data).hexdigest()
        return data

    def temporary(self) -> Path:
        return self.run / "scratch" / uuid4().hex

    def rollback_staging(self, checkpoint: int) -> None:
        """Discard an unsuccessful unit's private outputs, leaving its source intact."""
        while len(self.changes) > checkpoint:
            self.changes.popitem()
            (self.run / "new" / str(len(self.changes))).unlink(missing_ok=True)

    def remove_directory(self, relative: str) -> None:
        path = safe_path(self.home, relative, directory=True)
        before = digest(path, directory=True)
        if before is None:
            return
        if any(name == relative or name.startswith(relative + "/") for name in self.changes):
            raise UpgradeError("directory removal overlaps staged replacements")
        self.changes[relative] = Change(
            relative, before, None, stat.S_IMODE(path.stat().st_mode), directory=True
        )

    def stage(self, relative: str, source: bytes | Path | None) -> None:
        """Stage bytes or take ownership of a closed scratch file; None deletes."""
        original = self.source(relative)
        before = self.observed[relative]
        after = (
            hashlib.sha256(source).hexdigest()
            if isinstance(source, bytes)
            else digest(source, sync=not self._check_only)
            if source is not None
            else None
        )
        if before == after:
            if isinstance(source, Path):
                source.unlink()
            return
        index = len(self.changes)
        if relative in self.changes:
            raise UpgradeError("duplicate planned replacement")
        target = self.run / "new" / str(index)
        if isinstance(source, bytes):
            with target.open("wb") as stream:
                stream.write(source)
                if not self._check_only:
                    stream.flush()
                    os.fsync(stream.fileno())
        elif source is not None:
            replace_path_with_retry(source, target)
        self.changes[relative] = Change(
            relative,
            before,
            after,
            stat.S_IMODE(original.stat().st_mode) if before is not None else None,
        )

    def json(self, relative: str, value: object) -> None:
        data = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
        self.stage(relative, data.encode("utf-8"))

    def verify_source(self) -> None:
        total = len(self.changes)
        self.progress("verify_source", 0, total)
        for completed, change in enumerate(self.changes.values(), 1):
            # The existing file becomes the backup; sync its data while verifying it.
            path = safe_path(self.home, change.path, directory=change.directory)
            if digest(path, sync=True, directory=change.directory) != change.before:
                raise UpgradeError(f"source changed during upgrade: {change.path}")
            self.progress("verify_source", completed, total)

    def publish(self, report: dict[str, object]) -> None:
        if self._check_only:
            raise UpgradeError("a check-only workspace cannot publish")
        changes = list(self.changes.values())
        self.progress("prepare", 0, len(changes))
        write_json_atomic(
            self.run / "manifest.json",
            {
                "schema_version": 3,
                "changes": [asdict(change) for change in changes],
            },
        )
        for directory in (self.run / "new", self.run / "before", self.run):
            sync_directory(directory)
        self.progress("prepare", len(changes), len(changes))
        self.verify_source()
        # From this point even a failed marker write retains recovery materials.
        self.published = True
        write_json_atomic(self.marker, {"upgrade_id": self.identifier})
        sync_directory(self.root)
        self.progress("publish", 0, len(changes))
        for index, change in enumerate(changes):
            target = safe_path(self.home, change.path, directory=change.directory)
            if change.before is not None:
                backup = self.run / "before" / str(index)
                replace_path_with_retry(target, backup)
                restrict_to_user(backup, directory=change.directory)
                # Persist the original's new name before installing its replacement.
                sync_directory(backup.parent)
                sync_directory(target.parent)
            source = self.run / "new" / str(index) if change.after is not None else None
            if not change.directory:
                install(source, target, change.mode, consume=True)
            self.progress("publish", index + 1, len(changes))
        # Persist removal of the staged names as well as each destination rename.
        sync_directory(self.run / "new")
        self.progress("finalize")
        receipts = self.root / "receipts"
        reject_link(receipts)
        receipts.mkdir(exist_ok=True)
        write_json_atomic(receipts / f"{self.identifier}.json", report)
        sync_directory(receipts)
        self.marker.unlink()
        sync_directory(self.root)
        shutil.rmtree(self.run / "new")
        shutil.rmtree(self.run / "scratch")

    def restore(self) -> int:
        self.progress("verify")
        reject_link(self.marker)
        if not self.marker.exists():
            raise UpgradeError("no unfinished upgrade to restore")
        marker = loads(self.marker.read_text("utf-8"))
        if marker.get("operation") == "reset":
            raise UpgradeError("unfinished full reset; manual recovery is required")
        identifier = marker.get("upgrade_id")
        if (
            not isinstance(identifier, str)
            or len(identifier) != 32
            or any(character not in "0123456789abcdef" for character in identifier)
        ):
            raise UpgradeError("invalid upgrade marker")
        run = self.root / identifier
        for path in (run, run / "manifest.json", run / "before"):
            reject_link(path)
        manifest = loads((run / "manifest.json").read_text("utf-8"))
        try:
            version = manifest["schema_version"]
            if version not in (1, 2, 3):
                raise UpgradeError("unsupported upgrade manifest")
            changes = [Change(**item) for item in manifest["changes"]]
        except (KeyError, TypeError) as exc:
            raise UpgradeError("invalid upgrade manifest") from exc
        seen: set[str] = set()
        sources: list[Path | None] = []
        for index, change in enumerate(changes):
            target = safe_path(self.home, change.path, directory=change.directory)
            if change.directory and (
                version != 3 or change.before is None or change.after is not None
            ):
                raise UpgradeError("invalid directory removal in upgrade manifest")
            if change.path in seen:
                raise UpgradeError("duplicate path in upgrade manifest")
            seen.add(change.path)
            if change.before is not None:
                backup = run / "before" / str(index)
                reject_link(backup)
                # A version 2 publication may stop before moving this original.
                rollback = target if version >= 2 and not backup.exists() else backup
                if digest(rollback, directory=change.directory) != change.before:
                    raise UpgradeError(f"missing or damaged backup: {change.path}")
                sources.append(rollback)
            else:
                sources.append(None)
        self.progress("restore", 0, len(changes))
        for index, (change, source) in enumerate(zip(changes, sources, strict=True)):
            target = safe_path(self.home, change.path, directory=change.directory)
            if source != target:
                if change.directory:
                    assert source is not None
                    restore_directory(source, target, change.mode)
                else:
                    install(source, target, change.mode)
            self.progress("restore", index + 1, len(changes))
        self.progress("verify")
        for change in changes:
            if (
                digest(
                    safe_path(self.home, change.path, directory=change.directory),
                    directory=change.directory,
                )
                != change.before
            ):
                raise UpgradeError(f"restore verification failed: {change.path}")
        self.marker.unlink()
        sync_directory(self.root)
        return len(changes)
