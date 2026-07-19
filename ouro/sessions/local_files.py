from __future__ import annotations

import json
import os
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import MigrationRequiredError, PersistenceError, ResourceNotFoundError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference, SessionResourceReferences
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_locks import is_lock_held
from ouro.sessions.base import RuntimeSessionSnapshot, SessionSnapshot
from ouro.sessions.index import SessionIndex
from ouro.sessions.layout import DATA_LAYOUT_VERSION, DataLayoutManifest
from ouro.sessions.local_index import LocalSessionIndex
from ouro.sessions.local_io import write_json_atomic, write_model_atomic
from ouro.sessions.locks import SessionMutationLock, SessionRunLock

_STREAM_DELTA_EVENT_TYPES = {
    "model_text_delta",
    "model_reasoning_delta",
    "model_tool_call_delta",
}
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_JournalRecord = TypeVar("_JournalRecord", MessageRecord, EventRecord)


class LocalSessionStore:
    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.sessions_dir = self.root / "sessions"
        self.session_locks_dir = self.root / "locks" / "sessions"
        self.session_mutation_locks_dir = self.root / "locks" / "session-mutations"
        self.trash_sessions_dir = self.root / "trash" / "sessions"
        self._event_sequence_cache: dict[str, tuple[int, int]] = {}
        self._layout_path = self.root / "layout.json"
        self._catalog = LocalSessionIndex(
            self.sessions_dir,
            self.session_locks_dir,
            self._load_snapshot_for_index,
        )
        self._index_dirty_path = self._catalog.dirty_path

    def initialize(self) -> None:
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_locks_dir.mkdir(parents=True, exist_ok=True)
        self.session_mutation_locks_dir.mkdir(parents=True, exist_ok=True)
        self.trash_sessions_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_layout()
        self._recover_trash_staging()
        self._catalog.try_initialize()

    def create_session(self, session: SessionRecord) -> None:
        self.create_session_snapshot(session, [])

    def create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None = None,
    ) -> None:
        with self.session_mutation(session.session_id):
            self._create_session_snapshot(session, messages, resource_references)

    def _create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None,
    ) -> None:
        session_dir = self._session_dir(session.session_id)
        if session_dir.exists():
            raise PersistenceError(f"session already exists: {session.session_id}")

        staging_dir = self.sessions_dir / f".{session.session_id}.{new_id('stage')}.staging"
        try:
            (staging_dir / "runs").mkdir(parents=True)
            (staging_dir / "resources").mkdir(parents=True)
            self._write_model_atomic(staging_dir / "session.json", session)
            for name in ("messages.jsonl", "events.jsonl", "compactions.jsonl"):
                (staging_dir / name).touch()
            for message in messages:
                if message.session_id != session.session_id:
                    raise PersistenceError(
                        f"message {message.message_id} does not belong to "
                        f"session {session.session_id}"
                    )
                self._append_jsonl(staging_dir / "messages.jsonl", message)
            self._write_json_atomic(
                staging_dir / "resources" / "index.json",
                SessionResourceReferences(resources=resource_references or []).model_dump(
                    mode="json"
                ),
            )
            staging_dir.replace(session_dir)
            self._event_sequence_cache[session.session_id] = (0, 0)
        except (OSError, PersistenceError) as exc:
            with suppress(OSError):
                shutil.rmtree(staging_dir)
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"could not create session {session.session_id}: {exc}") from exc

    def save_session(self, session: SessionRecord) -> None:
        with self.session_mutation(session.session_id):
            session_dir = self._require_session_dir(session.session_id)
            self._write_model_atomic(session_dir / "session.json", session)

    def trash_session(self, session_id: str) -> str:
        with self.session_mutation(session_id):
            return self._trash_session(session_id)

    def _trash_session(self, session_id: str) -> str:
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            raise FileNotFoundError(session_dir)
        trash_id = new_id("trash")
        staging_dir = self.trash_sessions_dir / f".{trash_id}.staging"
        staged_session = staging_dir / "session"
        staged_manifest = staging_dir / "trash.json"
        target_dir = self.trash_sessions_dir / trash_id
        try:
            staging_dir.mkdir(parents=True)
            self._write_json_atomic(
                staged_manifest,
                {
                    "trash_id": trash_id,
                    "session_id": session_id,
                    "trashed_at": utc_now(),
                },
            )
            session_dir.replace(staged_session)
            staged_manifest.replace(staged_session / "trash.json")
            staged_session.replace(target_dir)
            staging_dir.rmdir()
        except (OSError, PersistenceError) as exc:
            if target_dir.exists() and not session_dir.exists():
                with suppress(OSError):
                    shutil.rmtree(staging_dir)
                return trash_id
            rollback_error: OSError | None = None
            if staged_session.exists() and not session_dir.exists():
                try:
                    with suppress(OSError):
                        (staged_session / "trash.json").unlink()
                    staged_session.replace(session_dir)
                except OSError as rollback_exc:
                    rollback_error = rollback_exc
            if rollback_error is None:
                with suppress(OSError):
                    shutil.rmtree(staging_dir)
            else:
                raise PersistenceError(
                    f"could not publish trash entry for session {session_id}; "
                    f"data retained at {staging_dir}: {rollback_error}"
                ) from exc
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"could not move session {session_id} to trash: {exc}") from exc
        return trash_id

    def append_message(self, message: MessageRecord) -> None:
        with self.session_mutation(message.session_id):
            session_dir = self._require_session_dir(message.session_id)
            self._append_jsonl(session_dir / "messages.jsonl", message)

    def append_event(self, event: EventRecord) -> None:
        with self.session_mutation(event.session_id):
            session_dir = self._require_session_dir(event.session_id)
            self._append_jsonl(session_dir / "events.jsonl", event)
            self._event_sequence_cache.pop(event.session_id, None)

    def append_ordered_event(self, event: EventRecord) -> EventRecord:
        with self.session_mutation(event.session_id):
            session_dir = self._require_session_dir(event.session_id)
            path = session_dir / "events.jsonl"
            sequence = max(
                event.sequence,
                self._max_event_sequence(event.session_id, path) + 1,
            )
            ordered = event.model_copy(update={"sequence": sequence})
            # Delta events are group-committed by the next durable boundary
            # event. This preserves ordered live replay without one fsync per token.
            self._append_jsonl(
                path,
                ordered,
                durable=ordered.event_type not in _STREAM_DELTA_EVENT_TYPES,
            )
            self._event_sequence_cache[event.session_id] = (path.stat().st_size, sequence)
            return ordered

    def save_run(self, run: RunRecord) -> None:
        with self.session_mutation(run.session_id):
            run_dir = self._require_session_dir(run.session_id) / "runs"
            run_dir.mkdir(parents=True, exist_ok=True)
            run_id = _validated_record_id(run.run_id, kind="run")
            self._write_model_atomic(run_dir / f"{run_id}.json", run)

    def save_latest_context(self, context: ContextSnapshotRecord) -> None:
        with self.session_mutation(context.session_id):
            session_dir = self._require_session_dir(context.session_id)
            self._write_model_atomic(session_dir / "context.json", context)

    def load_latest_context(self, session_id: str) -> ContextSnapshotRecord | None:
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "context.json"
            if not path.exists():
                return None
            try:
                return ContextSnapshotRecord.model_validate_json(path.read_text("utf-8"))
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(
                    f"could not load latest context for session {session_id}: {exc}"
                ) from exc

    def append_compaction(self, compaction: CompactionRecord) -> None:
        with self.session_mutation(compaction.session_id):
            session_dir = self._require_session_dir(compaction.session_id)
            self._append_jsonl(
                session_dir / "compactions.jsonl",
                compaction,
            )

    def activate_compaction(self, session_id: str, compaction_id: str) -> SessionRecord:
        with self.session_mutation(session_id):
            session_dir = self._require_session_dir(session_id)
            compactions = self._load_compactions(session_dir)
            if not any(item.compaction_id == compaction_id for item in compactions):
                raise PersistenceError(
                    f"unknown compaction {compaction_id} for session {session_id}"
                )
            updated = self.load_session_record(session_id).model_copy(
                update={
                    "active_compaction_id": compaction_id,
                    "updated_at": utc_now(),
                }
            )
            self.save_session(updated)
        self.refresh_catalog(session_id)
        return updated

    def load_session(self, session_id: str) -> SessionSnapshot:
        with self.session_mutation(session_id):
            runtime = self._load_runtime_session(session_id)
            session_dir = self._session_dir(session_id)
            try:
                events = [
                    EventRecord.model_validate_json(line)
                    for line in self._read_jsonl(session_dir / "events.jsonl")
                ]
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(f"could not load session {session_id}: {exc}") from exc
            snapshot = SessionSnapshot(
                **runtime.model_dump(),
                events=events,
            )
        return snapshot

    def load_runtime_session(self, session_id: str) -> RuntimeSessionSnapshot:
        with self.session_mutation(session_id):
            return self._load_runtime_session(session_id)

    def load_runs(self, session_id: str) -> list[RunRecord]:
        with self.session_mutation(session_id):
            return self._load_runs(session_id)

    def _load_runs(self, session_id: str) -> list[RunRecord]:
        session_dir = self._require_session_dir(session_id)
        try:
            runs = [
                RunRecord.model_validate_json(path.read_text("utf-8"))
                for path in sorted((session_dir / "runs").glob("*.json"))
            ]
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(f"could not load runs for session {session_id}: {exc}") from exc
        runs.sort(key=_run_sort_key)
        return runs

    def _load_runtime_session(self, session_id: str) -> RuntimeSessionSnapshot:
        session_dir = self._session_dir(session_id)
        try:
            session = self.load_session_record(session_id)
            messages = [
                MessageRecord.model_validate_json(line)
                for line in self._read_jsonl(session_dir / "messages.jsonl")
            ]
            compactions = self._load_compactions(session_dir)
        except FileNotFoundError:
            raise
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(f"could not load session {session_id}: {exc}") from exc
        runs = self._load_runs(session_id)
        snapshot = RuntimeSessionSnapshot(
            session=session,
            messages=messages,
            runs=runs,
            compactions=compactions,
        )
        if session.active_compaction_id is not None and snapshot.active_compaction is None:
            raise PersistenceError(
                f"session {session_id} references missing compaction {session.active_compaction_id}"
            )
        return snapshot

    def load_active_compaction(self, session_id: str) -> CompactionRecord | None:
        with self.session_mutation(session_id):
            session = self.load_session_record(session_id)
            active_id = session.active_compaction_id
            if active_id is None:
                return None
            session_dir = self._require_session_dir(session_id)
            active = next(
                (
                    compaction
                    for compaction in self._load_compactions(session_dir)
                    if compaction.compaction_id == active_id
                ),
                None,
            )
            if active is None:
                raise PersistenceError(
                    f"session {session_id} references missing compaction {active_id}"
                )
            return active

    def latest_event_sequence(self, session_id: str) -> int:
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "events.jsonl"
            return self._max_event_sequence(session_id, path)

    def load_session_record(self, session_id: str) -> SessionRecord:
        with self.session_mutation(session_id):
            path = self._session_dir(session_id) / "session.json"
            try:
                return SessionRecord.model_validate_json(path.read_text("utf-8"))
            except FileNotFoundError:
                raise
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(
                    f"could not load session record {session_id}: {exc}"
                ) from exc

    def load_messages_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> tuple[list[MessageRecord], int | None]:
        cursor, limit = _validated_page_args(cursor, limit)
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "messages.jsonl"
            return self._read_record_page(
                path,
                MessageRecord,
                cursor=cursor,
                limit=limit,
            )

    def load_events_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        run_id: str | None = None,
    ) -> tuple[list[EventRecord], int | None]:
        cursor, limit = _validated_page_args(cursor, limit)
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "events.jsonl"
            return self._read_record_page(
                path,
                EventRecord,
                cursor=cursor,
                limit=limit,
                run_id=run_id,
            )

    def load_events_after(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[list[EventRecord], bool]:
        after_sequence, limit = _validated_page_args(after_sequence, limit)
        path = self._require_session_dir(session_id) / "events.jsonl"
        records: list[EventRecord] = []
        try:
            with self.session_mutation(session_id), path.open("rb") as file:
                for raw in file:
                    if not raw.strip():
                        continue
                    event = EventRecord.model_validate_json(raw)
                    if event.sequence <= after_sequence:
                        continue
                    records.append(event)
                    if len(records) > limit:
                        return records[:limit], True
        except (OSError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise PersistenceError(f"could not read events after sequence: {exc}") from exc
        return records, False

    def load_run(self, session_id: str, run_id: str) -> RunRecord:
        with self.session_mutation(session_id):
            validated_run_id = _validated_record_id(run_id, kind="run")
            path = self._session_dir(session_id) / "runs" / f"{validated_run_id}.json"
            try:
                return RunRecord.model_validate_json(path.read_text("utf-8"))
            except FileNotFoundError:
                raise
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(
                    f"could not load run {run_id} for session {session_id}: {exc}"
                ) from exc

    def list_sessions(self) -> list[SessionRecord]:
        records: list[SessionRecord] = []
        for session_json in sorted(self.sessions_dir.glob("*/session.json")):
            try:
                records.append(SessionRecord.model_validate_json(session_json.read_text("utf-8")))
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(
                    f"could not load session record {session_json}: {exc}"
                ) from exc
        return records

    def load_resource_references(self, session_id: str) -> list[SessionResourceReference]:
        path = self._resource_reference_path(session_id)
        try:
            return SessionResourceReferences.model_validate_json(path.read_text("utf-8")).resources
        except FileNotFoundError:
            raise
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(
                f"could not load resource references for session {session_id}: {exc}"
            ) from exc

    def attach_resource_reference(
        self,
        session_id: str,
        reference: SessionResourceReference,
    ) -> None:
        with self.session_mutation(session_id):
            references = self.load_resource_references(session_id)
            if any(item.resource_id == reference.resource_id for item in references):
                return
            references.append(reference)
            self._write_model_atomic(
                self._resource_reference_path(session_id),
                SessionResourceReferences(resources=references),
            )

    def remove_resource_reference(self, session_id: str, resource_id: str) -> None:
        with self.session_mutation(session_id):
            references = self.load_resource_references(session_id)
            if not any(item.resource_id == resource_id for item in references):
                raise ResourceNotFoundError(
                    f"resource {resource_id} is not attached to session {session_id}"
                )
            self._write_model_atomic(
                self._resource_reference_path(session_id),
                SessionResourceReferences(
                    resources=[item for item in references if item.resource_id != resource_id]
                ),
            )

    def is_resource_referenced(self, resource_id: str) -> bool:
        for path in self.sessions_dir.glob("*/resources/index.json"):
            try:
                references = SessionResourceReferences.model_validate_json(
                    path.read_text("utf-8")
                ).resources
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(
                    f"could not load session resource references {path}: {exc}"
                ) from exc
            if any(reference.resource_id == resource_id for reference in references):
                return True
        return False

    def scan_catalog(self) -> SessionIndex:
        return self._catalog.scan()

    def rebuild_catalog(self) -> SessionIndex:
        return self._catalog.rebuild()

    def try_rebuild_catalog(self) -> str | None:
        return self._catalog.try_rebuild()

    def refresh_catalog(
        self,
        session_id: str,
        snapshot: RuntimeSessionSnapshot | None = None,
    ) -> str | None:
        return self._catalog.try_update(session_id, snapshot=snapshot)

    def read_catalog(self) -> SessionIndex:
        return self._catalog.load()

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionRunLock | None:
        lock = SessionRunLock(self._lock_path(session_id), session_id=session_id, run_id=run_id)
        try:
            return lock if lock.acquire() else None
        except OSError as exc:
            raise PersistenceError(
                f"could not acquire session lock for {session_id}: {exc}"
            ) from exc

    def session_mutation(self, session_id: str) -> SessionMutationLock:
        return SessionMutationLock(
            self._mutation_lock_path(session_id),
            session_id=session_id,
        )

    def is_session_locked(self, session_id: str) -> bool:
        try:
            return is_lock_held(self._lock_path(session_id))
        except OSError as exc:
            raise PersistenceError(
                f"could not inspect session lock for {session_id}: {exc}"
            ) from exc

    def _session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / _validated_record_id(session_id, kind="session")

    def _resource_reference_path(self, session_id: str) -> Path:
        return self._require_session_dir(session_id) / "resources" / "index.json"

    def _require_session_dir(self, session_id: str) -> Path:
        session_dir = self._session_dir(session_id)
        session_path = session_dir / "session.json"
        if not session_path.is_file():
            raise FileNotFoundError(session_path)
        return session_dir

    def _initialize_layout(self) -> None:
        if not self._layout_path.exists():
            self._write_model_atomic(self._layout_path, DataLayoutManifest())
            return
        try:
            manifest = DataLayoutManifest.model_validate_json(self._layout_path.read_text("utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(
                f"could not load data layout manifest {self._layout_path}: {exc}"
            ) from exc
        if manifest.data_layout_version != DATA_LAYOUT_VERSION:
            raise MigrationRequiredError(
                "OURO data layout migration required: "
                f"found {manifest.data_layout_version}, expected {DATA_LAYOUT_VERSION}"
            )

    def _load_snapshot_for_index(self, session_id: str) -> RuntimeSessionSnapshot:
        return self.load_runtime_session(session_id)

    def _recover_trash_staging(self) -> None:
        for staging_dir in self.trash_sessions_dir.glob(".*.staging"):
            staged_session = staging_dir / "session"
            staged_manifest = staging_dir / "trash.json"
            published_manifest = staged_session / "trash.json"
            if not staged_session.is_dir() or not (
                staged_manifest.is_file() or published_manifest.is_file()
            ):
                continue
            target_name = staging_dir.name.removeprefix(".").removesuffix(".staging")
            target_dir = self.trash_sessions_dir / target_name
            if target_dir.exists():
                continue
            try:
                if staged_manifest.exists():
                    staged_manifest.replace(published_manifest)
                staged_session.replace(target_dir)
                staging_dir.rmdir()
            except OSError as exc:
                raise PersistenceError(
                    f"could not recover staged trash entry {staging_dir}: {exc}"
                ) from exc

    def _lock_path(self, session_id: str) -> Path:
        validated = _validated_record_id(session_id, kind="session")
        return self.session_locks_dir / f"{validated}.lock"

    def _mutation_lock_path(self, session_id: str) -> Path:
        validated = _validated_record_id(session_id, kind="session")
        return self.session_mutation_locks_dir / f"{validated}.lock"

    def _max_event_sequence(self, session_id: str, path: Path) -> int:
        size = path.stat().st_size if path.exists() else 0
        cached = self._event_sequence_cache.get(session_id)
        if cached is not None and cached[0] == size:
            return cached[1]
        if size == 0:
            self._event_sequence_cache[session_id] = (0, 0)
            return 0
        try:
            line = _read_last_nonempty_line(path)
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("event record must be a JSON object")
            value = raw.get("sequence", 0)
            if not isinstance(value, int) or value < 0:
                raise ValueError("event sequence must be a non-negative integer")
        except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PersistenceError(f"could not read event sequence from {path}: {exc}") from exc
        self._event_sequence_cache[session_id] = (size, value)
        return value

    def _load_compactions(self, session_dir: Path) -> list[CompactionRecord]:
        return [
            CompactionRecord.model_validate_json(line)
            for line in self._read_jsonl(session_dir / "compactions.jsonl")
        ]

    def _write_model_atomic(self, path: Path, model: OuroModel) -> None:
        write_model_atomic(path, model)

    def _write_json_atomic(self, path: Path, value: object) -> None:
        write_json_atomic(path, value)

    def _append_jsonl(self, path: Path, model: OuroModel, *, durable: bool = True) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(model.model_dump_json())
                file.write("\n")
                file.flush()
                if durable:
                    os.fsync(file.fileno())
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(f"could not append {path}: {exc}") from exc

    def _read_jsonl(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            return [line for line in path.read_text("utf-8").splitlines() if line.strip()]
        except OSError as exc:
            raise PersistenceError(f"could not read {path}: {exc}") from exc

    def _read_record_page(
        self,
        path: Path,
        model_type: type[_JournalRecord],
        *,
        cursor: int,
        limit: int,
        run_id: str | None = None,
    ) -> tuple[list[_JournalRecord], int | None]:
        try:
            size = path.stat().st_size
            if cursor > size:
                raise ValueError(f"cursor {cursor} is beyond end of journal ({size})")
            with path.open("rb") as file:
                if cursor:
                    file.seek(cursor - 1)
                    if file.read(1) != b"\n":
                        raise ValueError(f"cursor {cursor} is not at a record boundary")
                file.seek(cursor)
                records: list[_JournalRecord] = []
                while len(records) < limit:
                    raw = file.readline()
                    if not raw:
                        break
                    if not raw.strip():
                        continue
                    record = model_type.model_validate_json(raw)
                    if run_id is not None and getattr(record, "run_id", None) != run_id:
                        continue
                    records.append(record)
                position = file.tell()
            return records, position if position < size else None
        except (OSError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise PersistenceError(f"could not read journal page from {path}: {exc}") from exc


def _run_sort_key(run: RunRecord) -> tuple[str, str]:
    return (str(run.started_at), run.run_id)


def _validated_record_id(value: str, *, kind: str) -> str:
    if not _RECORD_ID.fullmatch(value):
        raise ValueError(f"invalid {kind} id: {value!r}")
    return value


def _validated_page_args(cursor: int, limit: int) -> tuple[int, int]:
    if cursor < 0:
        raise ValueError("cursor must be non-negative")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return cursor, limit


def _read_last_nonempty_line(path: Path, *, chunk_size: int = 8192) -> str:
    """Read the ordered journal tail without materializing the complete file."""

    with path.open("rb") as file:
        position = file.seek(0, os.SEEK_END)
        buffer = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            file.seek(position)
            buffer = file.read(read_size) + buffer
            lines = buffer.splitlines()
            if position > 0 and len(lines) <= 1:
                continue
            for raw in reversed(lines):
                if raw.strip():
                    return raw.decode("utf-8")
        return ""
