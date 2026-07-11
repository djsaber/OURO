from __future__ import annotations

import os
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.errors import MigrationRequiredError, PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.base import SessionSnapshot
from ouro.sessions.index import SessionIndex
from ouro.sessions.layout import DATA_LAYOUT_VERSION, DataLayoutManifest
from ouro.sessions.local_index import LocalSessionIndex
from ouro.sessions.local_io import write_json_atomic, write_model_atomic
from ouro.sessions.locks import SessionRunLock, is_lock_held

_INCOMPLETE_RUN_STATUSES = {"queued", "running"}
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class LocalSessionStore:
    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.sessions_dir = self.root / "sessions"
        self.session_locks_dir = self.root / "locks" / "sessions"
        self.trash_sessions_dir = self.root / "trash" / "sessions"
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
            self._write_json_atomic(staging_dir / "resources" / "index.json", {"resources": []})
            staging_dir.replace(session_dir)
        except (OSError, PersistenceError) as exc:
            with suppress(OSError):
                shutil.rmtree(staging_dir)
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"could not create session {session.session_id}: {exc}") from exc

    def save_session(self, session: SessionRecord) -> None:
        session_dir = self._require_session_dir(session.session_id)
        self._write_model_atomic(session_dir / "session.json", session)

    def trash_session(self, session_id: str) -> str:
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
        session_dir = self._require_session_dir(message.session_id)
        self._append_jsonl(session_dir / "messages.jsonl", message)

    def append_event(self, event: EventRecord) -> None:
        session_dir = self._require_session_dir(event.session_id)
        self._append_jsonl(session_dir / "events.jsonl", event)

    def save_run(self, run: RunRecord) -> None:
        run_dir = self._require_session_dir(run.session_id) / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = _validated_record_id(run.run_id, kind="run")
        self._write_model_atomic(run_dir / f"{run_id}.json", run)

    def append_compaction(self, compaction: CompactionRecord) -> None:
        session_dir = self._require_session_dir(compaction.session_id)
        self._append_jsonl(
            session_dir / "compactions.jsonl",
            compaction,
        )

    def activate_compaction(self, session_id: str, compaction_id: str) -> SessionRecord:
        snapshot = self.load_session(session_id, recover=False)
        if not any(item.compaction_id == compaction_id for item in snapshot.compactions):
            raise PersistenceError(f"unknown compaction {compaction_id} for session {session_id}")
        updated = snapshot.session.model_copy(
            update={
                "active_compaction_id": compaction_id,
                "updated_at": utc_now(),
            }
        )
        self.save_session(updated)
        self.try_update_index(session_id)
        return updated

    def load_session(self, session_id: str, *, recover: bool = True) -> SessionSnapshot:
        session_dir = self._session_dir(session_id)
        try:
            session = self.load_session_record(session_id)
            messages = [
                MessageRecord.model_validate_json(line)
                for line in self._read_jsonl(session_dir / "messages.jsonl")
            ]
            events = [
                EventRecord.model_validate_json(line)
                for line in self._read_jsonl(session_dir / "events.jsonl")
            ]
            runs = [
                RunRecord.model_validate_json(path.read_text("utf-8"))
                for path in sorted((session_dir / "runs").glob("*.json"))
            ]
            compactions = [
                CompactionRecord.model_validate_json(line)
                for line in self._read_jsonl(session_dir / "compactions.jsonl")
            ]
        except FileNotFoundError:
            raise
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(f"could not load session {session_id}: {exc}") from exc
        runs.sort(key=_run_sort_key)
        if recover:
            session, messages, events, runs = self._recover_incomplete_runs(
                session,
                messages,
                events,
                runs,
            )
        snapshot = SessionSnapshot(
            session=session,
            messages=messages,
            events=events,
            runs=runs,
            compactions=compactions,
        )
        if session.active_compaction_id is not None and snapshot.active_compaction is None:
            raise PersistenceError(
                f"session {session_id} references missing compaction {session.active_compaction_id}"
            )
        return snapshot

    def load_session_record(self, session_id: str) -> SessionRecord:
        path = self._session_dir(session_id) / "session.json"
        try:
            return SessionRecord.model_validate_json(path.read_text("utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(f"could not load session record {session_id}: {exc}") from exc

    def load_run(self, session_id: str, run_id: str) -> RunRecord:
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

    def scan_index(self) -> SessionIndex:
        return self._catalog.scan()

    def rebuild_index(self) -> SessionIndex:
        return self._catalog.rebuild()

    def try_rebuild_index(self) -> str | None:
        return self._catalog.try_rebuild()

    def try_update_index(self, session_id: str) -> str | None:
        return self._catalog.try_update(session_id)

    def load_index(self) -> SessionIndex:
        return self._catalog.load()

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionRunLock | None:
        lock = SessionRunLock(self._lock_path(session_id), session_id=session_id, run_id=run_id)
        try:
            return lock if lock.acquire() else None
        except OSError as exc:
            raise PersistenceError(
                f"could not acquire session lock for {session_id}: {exc}"
            ) from exc

    def is_session_locked(self, session_id: str) -> bool:
        try:
            return is_lock_held(self._lock_path(session_id))
        except OSError as exc:
            raise PersistenceError(
                f"could not inspect session lock for {session_id}: {exc}"
            ) from exc

    def _recover_incomplete_runs(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        events: list[EventRecord],
        runs: list[RunRecord],
    ) -> tuple[SessionRecord, list[MessageRecord], list[EventRecord], list[RunRecord]]:
        if self.is_session_locked(session.session_id):
            return session, messages, events, runs

        recovered_runs: list[RunRecord] = []
        changed = False
        for run in runs:
            if run.status in _INCOMPLETE_RUN_STATUSES:
                metadata = dict(run.metadata)
                metadata.pop("checkpoint", None)
                partial_message = self._append_recovered_partial_message(
                    session=session,
                    run=run,
                    messages=messages,
                    events=events,
                )
                if partial_message is not None:
                    metadata["recovered_partial_assistant_message_id"] = partial_message.message_id
                metadata["recovered_from_status"] = run.status
                recovered = run.model_copy(
                    update={
                        "status": "recoverable",
                        "ended_at": utc_now(),
                        "error": "Run was interrupted before completion.",
                        "abort_requested": False,
                        "metadata": metadata,
                    }
                )
                self.save_run(recovered)
                events.append(
                    self._append_recovery_event(
                        session_id=session.session_id,
                        run_id=recovered.run_id,
                        previous_status=run.status,
                        sequence=len(events) + 1,
                    )
                )
                recovered_runs.append(recovered)
                changed = True
            else:
                recovered_runs.append(run)

        if changed:
            metadata = dict(session.metadata)
            metadata["recovered_incomplete_runs"] = True
            session_update = {
                "updated_at": utc_now(),
                "last_activity_at": utc_now(),
                "active_run_id": None,
                "last_message_id": messages[-1].message_id if messages else session.last_message_id,
                "message_count": len(messages),
                "metadata": metadata,
            }
            if session.status == "running":
                session_update["status"] = "idle"
            session = session.model_copy(update=session_update)
            self.save_session(session)
            self.try_update_index(session.session_id)

        return session, messages, events, recovered_runs

    def _append_recovered_partial_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        events: list[EventRecord],
    ) -> MessageRecord | None:
        run_events = sorted(
            (event for event in events if event.run_id == run.run_id),
            key=lambda event: (event.sequence, event.created_at, event.event_id),
        )
        start_indexes = [
            index
            for index, event in enumerate(run_events)
            if event.event_type == "model_request_started"
        ]
        if not start_indexes:
            return None
        start_index = start_indexes[-1]
        start_event = run_events[start_index]
        streamed_events = run_events[start_index + 1 :]
        if any(
            event.event_type
            in {
                "tool_call_requested",
                "approval_requested",
                "ask_user_requested",
                "tool_call_completed",
            }
            for event in streamed_events
        ):
            return None
        durable_run_messages = [message for message in messages if message.run_id == run.run_id]
        if durable_run_messages and (
            durable_run_messages[-1].role in {"assistant", "tool_result"}
            and durable_run_messages[-1].created_at >= start_event.created_at
        ):
            return None

        text = "".join(
            value
            for event in streamed_events
            if event.event_type == "model_text_delta"
            and isinstance((value := event.payload.get("text")), str)
        )
        reasoning = "".join(
            value
            for event in streamed_events
            if event.event_type == "model_reasoning_delta"
            and isinstance((value := event.payload.get("reasoning_content")), str)
        )
        if not text and not reasoning:
            return None
        metadata: dict[str, Any] = {
            "incomplete": True,
            "recovered_from_event_journal": True,
            "run_id": run.run_id,
        }
        if reasoning:
            metadata["reasoning_content"] = reasoning
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=len(messages) + 1,
        ).model_copy(
            update={
                "metadata": metadata,
            }
        )
        self.append_message(message)
        messages.append(message)
        return message

    def _append_recovery_event(
        self,
        *,
        session_id: str,
        run_id: str,
        previous_status: str,
        sequence: int,
    ) -> EventRecord:
        event = EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type="run_status_changed",
            sequence=sequence,
            payload={
                "from_status": previous_status,
                "to_status": "recoverable",
                "reason": "process_restarted",
            },
        )
        self.append_event(event)
        return event

    def _session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / _validated_record_id(session_id, kind="session")

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

    def _load_snapshot_for_index(self, session_id: str) -> SessionSnapshot:
        return self.load_session(session_id, recover=False)

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

    def _write_model_atomic(self, path: Path, model: OuroModel) -> None:
        write_model_atomic(path, model)

    def _write_json_atomic(self, path: Path, value: object) -> None:
        write_json_atomic(path, value)

    def _append_jsonl(self, path: Path, model: OuroModel) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(model.model_dump_json())
                file.write("\n")
                file.flush()
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


def _run_sort_key(run: RunRecord) -> tuple[str, str]:
    return (str(run.started_at), run.run_id)


def _validated_record_id(value: str, *, kind: str) -> str:
    if not _RECORD_ID.fullmatch(value):
        raise ValueError(f"invalid {kind} id: {value!r}")
    return value
