from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ouro.core.base import utc_now
from ouro.core.errors import PersistenceError
from ouro.local_locks import FileMutationLock
from ouro.plugins.models import PluginAuditEvent, PluginStateDocument
from ouro.plugins.package import (
    ValidatedPluginPackage,
    compute_tree_digest,
    validate_package_tree,
)


class PluginStoreError(PersistenceError):
    pass


class PluginStateConflictError(PluginStoreError):
    pass


class PluginStateFormatError(PluginStoreError):
    """The state bytes are readable but violate the current state contract."""


class PluginStoreIOError(PluginStoreError):
    """The plugin store could not complete a filesystem operation."""


@dataclass(frozen=True)
class PluginStateRecovery:
    """Boot-time read of plugin state, including any quarantine that produced it."""

    state: PluginStateDocument
    quarantine_path: Path | None = None
    error: str | None = None


class PluginPackageStore:
    """Immutable package bytes, mutable state, data and audit with separate lifecycles."""

    def __init__(self, home: str | Path) -> None:
        self.home = Path(home).expanduser().resolve()
        self.root = self.home / "plugins"
        self.store_root = self.root / "store"
        self.data_root = self.root / "data"
        self.staging_root = self.root / "staging"
        self.lock_root = self.root / "locks"
        self.state_path = self.root / "state.json"
        self.audit_path = self.root / "audit.jsonl"

    def initialize(self) -> None:
        for path in (
            self.store_root,
            self.data_root,
            self.staging_root,
            self.lock_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def package_path(self, plugin_id: str, package_digest: str) -> Path:
        return self.store_root / plugin_id / package_digest

    def verify_package(self, plugin_id: str, package_digest: str) -> Path:
        """Verify immutable package bytes immediately before they are trusted."""

        package = self.package_path(plugin_id, package_digest)
        if not package.is_dir():
            raise PluginStoreError(f"plugin package is missing: {plugin_id}@{package_digest}")
        try:
            validate_package_tree(package)
            actual = compute_tree_digest(package)
        except (OSError, ValueError) as exc:
            raise PluginStoreError(
                f"immutable plugin store is invalid for {plugin_id}: {exc}"
            ) from exc
        if actual != package_digest:
            raise PluginStoreError(f"immutable plugin store is corrupted for {plugin_id}")
        return package

    def publish(self, package: ValidatedPluginPackage) -> Path:
        self.initialize()
        target = self.package_path(package.manifest.plugin_id, package.package_digest)
        with self.plugin_lock(package.manifest.plugin_id):
            if target.exists():
                actual = compute_tree_digest(target)
                if actual != package.package_digest:
                    raise PluginStoreError(
                        f"immutable plugin store is corrupted for {package.manifest.plugin_id}"
                    )
                return target
            staging = self.staging_root / f"{package.package_digest}.{uuid4().hex}"
            try:
                shutil.copytree(package.root, staging)
                _fsync_tree(staging)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
                _fsync_directory(target.parent)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return target

    def read_state(self) -> PluginStateDocument:
        if not self.state_path.exists():
            return PluginStateDocument()
        try:
            content = self.state_path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise PluginStateFormatError(f"invalid plugin state encoding: {exc}") from exc
        except OSError as exc:
            raise PluginStoreIOError(f"could not read plugin state: {exc}") from exc
        try:
            raw = json.loads(content)
            return PluginStateDocument.model_validate(_migrate_plugin_state(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise PluginStateFormatError(f"invalid plugin state: {exc}") from exc

    def recover_state(self) -> PluginStateRecovery:
        """Read plugin state for Host boot, quarantining an unreadable document.

        The plugin subsystem is optional to the Host: an unreadable state
        document (old schema, truncated write, corruption) must degrade the
        subsystem to an empty registry instead of failing the boot.  The
        rejected file is renamed, never deleted, so the event stays auditable
        and recorded grants remain inspectable.  Mutation paths keep using
        ``read_state`` and stay strict: they must never build on top of a
        registry they could not parse.
        """

        self.initialize()
        with FileMutationLock(self.lock_root / "state-mutations.lock"):
            try:
                return PluginStateRecovery(state=self.read_state())
            except PluginStateFormatError as exc:
                return PluginStateRecovery(
                    state=PluginStateDocument(),
                    quarantine_path=self._quarantine_state_document(),
                    error=str(exc),
                )

    def mutate_state(
        self,
        operation: Callable[[PluginStateDocument], PluginStateDocument],
    ) -> PluginStateDocument:
        self.initialize()
        with FileMutationLock(self.lock_root / "state-mutations.lock"):
            current = self.read_state()
            updated = operation(current.model_copy(deep=True))
            self._write_state_unlocked(updated)
            return updated

    def commit_candidate_state(
        self,
        expected: PluginStateDocument,
        candidate: PluginStateDocument,
    ) -> PluginStateDocument:
        """Commit a fully validated candidate only if its source state is unchanged."""

        self.initialize()
        with FileMutationLock(self.lock_root / "state-mutations.lock"):
            current = self.read_state()
            if current != expected:
                raise PluginStateConflictError(
                    "plugin state changed while an activation candidate was building"
                )
            committed = PluginStateDocument.model_validate(candidate.model_dump(mode="python"))
            if committed != current:
                self._write_state_unlocked(committed)
            return committed

    def append_audit(self, event: PluginAuditEvent) -> None:
        self.initialize()
        try:
            with FileMutationLock(self.lock_root / "audit-mutations.lock"):
                with self.audit_path.open("a", encoding="utf-8") as stream:
                    stream.write(event.model_dump_json(exclude_none=True))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            # Audit must not invalidate an already committed package/state transaction.
            return

    def audit_events(self, *, limit: int = 100) -> list[PluginAuditEvent]:
        if not self.audit_path.exists():
            return []
        events: list[PluginAuditEvent] = []
        for line in self.audit_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(PluginAuditEvent.model_validate_json(line))
        return events[-limit:]

    def garbage_collect(self, referenced: set[tuple[str, str]]) -> list[Path]:
        removed: list[Path] = []
        if not self.store_root.exists():
            return removed
        for plugin_dir in self.store_root.iterdir():
            if not plugin_dir.is_dir():
                continue
            with self.plugin_lock(plugin_dir.name):
                for digest_dir in plugin_dir.iterdir():
                    if digest_dir.is_dir() and (plugin_dir.name, digest_dir.name) not in referenced:
                        shutil.rmtree(digest_dir)
                        removed.append(digest_dir)
                if not any(plugin_dir.iterdir()):
                    plugin_dir.rmdir()
        for orphan in self.staging_root.glob("*"):
            if orphan.is_dir():
                shutil.rmtree(orphan)
                removed.append(orphan)
            else:
                orphan.unlink()
                removed.append(orphan)
        return removed

    def purge_data(self, plugin_id: str) -> None:
        data = self.data_root / plugin_id
        if data.exists():
            shutil.rmtree(data)

    def plugin_lock(self, plugin_id: str) -> FileMutationLock:
        return FileMutationLock(self.lock_root / f"{plugin_id}.lock")

    def _quarantine_state_document(self) -> Path:
        stamp = f"{utc_now():%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        target = self.state_path.with_name(f"{self.state_path.stem}.invalid-{stamp}.json")
        try:
            self.state_path.rename(target)
        except OSError as exc:
            raise PluginStoreIOError(f"could not quarantine invalid plugin state: {exc}") from exc
        return target

    def _write_state_unlocked(self, state: PluginStateDocument) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = PluginStateDocument.model_validate(state.model_dump(mode="python"))
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.state_path.parent,
                delete=False,
            ) as stream:
                stream.write(state.model_dump_json(indent=2, exclude_none=True))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                temporary = Path(stream.name)
            os.replace(temporary, self.state_path)
            _fsync_directory(self.state_path.parent)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _migrate_plugin_state(raw: object) -> object:
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        return raw
    migrated = dict(raw)
    migrated["schema_version"] = 3
    migrated.setdefault("builtin_activation_overrides", {})
    return migrated


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            # Windows' ``_commit`` (used by ``os.fsync``) rejects a descriptor
            # opened read-only with ``EBADF``.  Opening the staged copy for
            # update does not alter its bytes and keeps the durability barrier
            # meaningful on every supported platform.
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
