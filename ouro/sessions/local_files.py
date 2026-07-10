from __future__ import annotations

import importlib
import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import Self

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.base import SessionSnapshot

_INCOMPLETE_RUN_STATUSES = {"queued", "running"}
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class LocalSessionStore:
    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.sessions_dir = self.root / "sessions"

    def initialize(self) -> None:
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        index_path = self.sessions_dir / "index.json"
        if not index_path.exists():
            self._write_json_atomic(index_path, {"sessions": []})
        else:
            self.load_index()

    def create_session(self, session: SessionRecord) -> None:
        session_dir = self._session_dir(session.session_id)
        (session_dir / "runs").mkdir(parents=True, exist_ok=True)
        (session_dir / "resources").mkdir(parents=True, exist_ok=True)
        self.save_session(session)
        for name in ("messages.jsonl", "events.jsonl"):
            (session_dir / name).touch(exist_ok=True)
        self._write_json_atomic(session_dir / "resources" / "index.json", {"resources": []})
        self.rebuild_index()

    def save_session(self, session: SessionRecord) -> None:
        session_dir = self._session_dir(session.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._write_model_atomic(session_dir / "session.json", session)

    def append_message(self, message: MessageRecord) -> None:
        self._append_jsonl(self._session_dir(message.session_id) / "messages.jsonl", message)

    def append_event(self, event: EventRecord) -> None:
        self._append_jsonl(self._session_dir(event.session_id) / "events.jsonl", event)

    def save_run(self, run: RunRecord) -> None:
        run_dir = self._session_dir(run.session_id) / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = _validated_record_id(run.run_id, kind="run")
        self._write_model_atomic(run_dir / f"{run_id}.json", run)

    def load_session(self, session_id: str, *, recover: bool = True) -> SessionSnapshot:
        session_dir = self._session_dir(session_id)
        session = SessionRecord.model_validate_json(
            (session_dir / "session.json").read_text("utf-8")
        )
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
        runs.sort(key=_run_sort_key)
        if recover:
            session, messages, events, runs = self._recover_incomplete_runs(
                session,
                messages,
                events,
                runs,
            )
        return SessionSnapshot(session=session, messages=messages, events=events, runs=runs)

    def list_sessions(self) -> list[SessionRecord]:
        records: list[SessionRecord] = []
        for session_json in sorted(self.sessions_dir.glob("*/session.json")):
            records.append(SessionRecord.model_validate_json(session_json.read_text("utf-8")))
        return records

    def rebuild_index(self) -> None:
        sessions = [
            {
                "session_id": session.session_id,
                "title": session.title,
                "updated_at": session.updated_at,
                "status": session.status,
                "workspace": session.workspace,
                "current_model": session.current_model,
                "archived": session.archived,
            }
            for session in self.list_sessions()
        ]
        self._write_json_atomic(self.sessions_dir / "index.json", {"sessions": sessions})

    def load_index(self) -> dict[str, object]:
        index_path = self.sessions_dir / "index.json"
        try:
            raw = json.loads(index_path.read_text("utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), list):
                raise ValueError("session index has invalid shape")
            return raw
        except (OSError, json.JSONDecodeError, ValueError):
            self.rebuild_index()
            return json.loads(index_path.read_text("utf-8"))

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionRunLock | None:
        lock = SessionRunLock(self._lock_path(session_id), session_id=session_id, run_id=run_id)
        return lock if lock.acquire() else None

    def is_session_locked(self, session_id: str) -> bool:
        path = self._lock_path(session_id)
        if not path.exists():
            return False
        try:
            descriptor = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            if not _try_lock_descriptor(descriptor):
                return True
            _unlock_descriptor(descriptor)
            return False
        finally:
            os.close(descriptor)

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
                partial_message = self._append_recovered_partial_message(
                    session=session,
                    run=run,
                    messages=messages,
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
                "last_message_id": messages[-1].message_id if messages else session.last_message_id,
                "message_count": len(messages),
                "metadata": metadata,
            }
            if session.status == "running":
                session_update["status"] = "idle"
            session = session.model_copy(update=session_update)
            self.save_session(session)
            self.rebuild_index()

        return session, messages, events, recovered_runs

    def _append_recovered_partial_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
    ) -> MessageRecord | None:
        checkpoint = run.metadata.get("checkpoint")
        if not isinstance(checkpoint, dict):
            return None
        text = checkpoint.get("partial_assistant_text")
        if not isinstance(text, str) or not text:
            return None
        raw_message_id = checkpoint.get("partial_assistant_message_id")
        message_id = raw_message_id if isinstance(raw_message_id, str) else new_id("msg")
        if any(message.message_id == message_id for message in messages):
            return None
        raw_sequence = checkpoint.get("partial_assistant_sequence")
        sequence = raw_sequence if isinstance(raw_sequence, int) else len(messages) + 1
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=max(sequence, len(messages) + 1),
        ).model_copy(
            update={
                "message_id": message_id,
                "metadata": {
                    "incomplete": True,
                    "recovered_from_checkpoint": True,
                    "run_id": run.run_id,
                },
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

    def _lock_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / ".active_run.lock"

    def _write_model_atomic(self, path: Path, model: OuroModel) -> None:
        self._write_text_atomic(path, model.model_dump_json(indent=2))

    def _write_json_atomic(self, path: Path, value: object) -> None:
        self._write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2, default=str))

    def _write_text_atomic(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp.write(text)
            tmp.write("\n")
            temp_name = tmp.name
        Path(temp_name).replace(path)

    def _append_jsonl(self, path: Path, model: OuroModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(model.model_dump_json())
            file.write("\n")

    def _read_jsonl(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        return [line for line in path.read_text("utf-8").splitlines() if line.strip()]


def _run_sort_key(run: RunRecord) -> tuple[str, str]:
    return (str(run.started_at), run.run_id)


class SessionRunLock:
    def __init__(self, path: Path, *, session_id: str, run_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self.run_id = run_id
        self._acquired = False
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR)
        if not _try_lock_descriptor(descriptor):
            os.close(descriptor)
            return False
        try:
            payload = json.dumps(
                {
                    "session_id": self.session_id,
                    "run_id": self.run_id,
                    "pid": os.getpid(),
                    "created_at": utc_now(),
                },
                default=str,
            )
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, payload.encode("utf-8"))
            os.write(descriptor, b"\n")
            os.fsync(descriptor)
        except Exception:
            _unlock_descriptor(descriptor)
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._acquired = True
        return True

    def release(self) -> None:
        if not self._acquired or self._descriptor is None:
            return
        try:
            _unlock_descriptor(self._descriptor)
        finally:
            os.close(self._descriptor)
            self._descriptor = None
            self._acquired = False

    def __enter__(self) -> Self:
        if not self._acquired and not self.acquire():
            raise RuntimeError(f"session is already locked: {self.session_id}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.release()


def _validated_record_id(value: str, *, kind: str) -> str:
    if not _RECORD_ID.fullmatch(value):
        raise ValueError(f"invalid {kind} id: {value!r}")
    return value


def _try_lock_descriptor(descriptor: int) -> bool:
    _ensure_lock_byte(descriptor)
    try:
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    fcntl = importlib.import_module("fcntl")
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _ensure_lock_byte(descriptor: int) -> None:
    if os.fstat(descriptor).st_size > 0:
        return
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, b"\0")
    os.fsync(descriptor)
