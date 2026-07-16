from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ouro.local_locks import FileMutationLock
from ouro.plugins.models import PluginAuditEvent, PluginStateDocument
from ouro.plugins.package import (
    ValidatedPluginPackage,
    compute_tree_digest,
    validate_package_tree,
)


class PluginStoreError(RuntimeError):
    pass


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
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return PluginStateDocument.model_validate(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise PluginStoreError(f"invalid plugin state: {exc}") from exc

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

    def _write_state_unlocked(self, state: PluginStateDocument) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
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
