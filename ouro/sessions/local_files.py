from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import ValidationError

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.base import SessionSnapshot
from ouro.sessions.locks import SessionRunLock, is_lock_held

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
        for name in ("messages.jsonl", "events.jsonl", "compactions.jsonl"):
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

    def append_compaction(self, compaction: CompactionRecord) -> None:
        self._append_jsonl(
            self._session_dir(compaction.session_id) / "compactions.jsonl",
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
        self.rebuild_index()
        return updated

    def load_session(self, session_id: str, *, recover: bool = True) -> SessionSnapshot:
        session_dir = self._session_dir(session_id)
        try:
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
            try:
                return json.loads(index_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise PersistenceError(
                    f"could not rebuild session index {index_path}: {exc}"
                ) from exc

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
        if not isinstance(text, str):
            text = ""
        reasoning = checkpoint.get("partial_assistant_reasoning")
        if not isinstance(reasoning, str):
            reasoning = ""
        if not text and not reasoning:
            return None
        raw_message_id = checkpoint.get("partial_assistant_message_id")
        message_id = raw_message_id if isinstance(raw_message_id, str) else new_id("msg")
        if any(message.message_id == message_id for message in messages):
            return None
        raw_sequence = checkpoint.get("partial_assistant_sequence")
        sequence = raw_sequence if isinstance(raw_sequence, int) else len(messages) + 1
        metadata: dict[str, Any] = {
            "incomplete": True,
            "recovered_from_checkpoint": True,
            "run_id": run.run_id,
        }
        if reasoning:
            metadata["reasoning_content"] = reasoning
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=max(sequence, len(messages) + 1),
        ).model_copy(
            update={
                "message_id": message_id,
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

    def _lock_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / ".active_run.lock"

    def _write_model_atomic(self, path: Path, model: OuroModel) -> None:
        self._write_text_atomic(path, model.model_dump_json(indent=2))

    def _write_json_atomic(self, path: Path, value: object) -> None:
        self._write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2, default=str))

    def _write_text_atomic(self, path: Path, text: str) -> None:
        temp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
            ) as tmp:
                tmp.write(text)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
                temp_path = Path(tmp.name)
            temp_path.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(f"could not write {path}: {exc}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                with suppress(OSError):
                    temp_path.unlink()

    def _append_jsonl(self, path: Path, model: OuroModel) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(model.model_dump_json())
                file.write("\n")
                file.flush()
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
