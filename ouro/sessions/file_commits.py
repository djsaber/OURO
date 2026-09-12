from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionRecord
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessVersion
from ouro.core.message_frames import MessageTruncation, retain_compactions
from ouro.core.messages import MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.session_commits import (
    HistoryBoundaryUnavailable,
    PreparedSessionCommit,
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
    prepare_session_commit,
)
from ouro.core.state import SessionRecord
from ouro.local_io import read_text_atomic, write_model_atomic, write_text_atomic
from ouro.sessions.message_index import MessageJournalQueries
from ouro.sessions.message_journal import reverse_jsonl


class FileCommitDecision(PreparedSessionCommit):
    message_offset: int = Field(ge=0)
    event_offset: int = Field(ge=0)


class FileSessionCommits:
    """Apply one Session decision under the adapter's existing mutation lock."""

    def __init__(self, directory: Callable[[str], Path], queries: MessageJournalQueries) -> None:
        self.directory = directory
        self.queries = queries

    def load_session(self, session_id: str) -> SessionRecord:
        session = decode_versioned_record_json(
            SessionRecord,
            read_text_atomic(self.directory(session_id) / "session.json"),
            label=f"session record {session_id}",
        )
        if session.session_id != session_id:
            raise PersistenceError("Session record has a foreign identity")
        version = self.load_harness(session_id, session.harness_version)
        session.controls = version.state.controls.model_copy(deep=True)
        return session

    def load_harness(self, session_id: str, version_id: str) -> HarnessVersion:
        path = self._version_path(session_id, version_id)
        try:
            version = decode_versioned_record_json(
                HarnessVersion, read_text_atomic(path), label=f"Harness {version_id}"
            )
        except FileNotFoundError as exc:
            raise PersistenceError(f"Harness checkpoint not found: {version_id}") from exc
        if version.session_id != session_id or version.version_id != version_id:
            raise PersistenceError("Harness checkpoint has a foreign identity")
        return version

    def load_result(self, session_id: str, operation_id: str) -> SessionCommitResult | None:
        path = self._result_path(session_id, operation_id)
        if not path.exists():
            return None
        result = decode_versioned_record_json(
            SessionCommitResult, read_text_atomic(path), label=f"Session commit {operation_id}"
        )
        if result.session.session_id != session_id or result.operation_id != operation_id:
            raise PersistenceError("Session commit result has a foreign identity")
        return result

    def commit(self, request: SessionCommit) -> SessionCommitResult:
        request = SessionCommit.model_validate(request.model_dump(mode="json"))
        session_id = request.before.session_id
        previous = self.load_result(session_id, request.operation_id)
        if previous is not None:
            if previous.request_digest != request.digest:
                raise SessionCommitConflict("operation identity was reused for different effects")
            return previous
        directory = self.directory(session_id)
        current = self.load_session(session_id)
        harness = self.load_harness(session_id, current.harness_version)
        message_path = directory / "messages.jsonl"
        event_path = directory / "events.jsonl"
        message_offset = message_path.stat().st_size
        event_offset = event_path.stat().st_size
        event_sequence = 0
        with closing(reverse_jsonl(event_path)) as lines:
            _, line = next(lines, (0, b""))
        if line:
            event_sequence = decode_versioned_record_json(
                EventRecord, line, label=f"event tail for {session_id}"
            ).sequence
        truncation = None
        restore = None
        compactions = None
        if request.before_message_id is not None:
            truncation, message_offset, removed_ids = self._locate_cut(request)
            version_id = truncation.first_removed.before_harness_version
            if version_id is None:
                raise HistoryBoundaryUnavailable("message has no Harness checkpoint")
            restore = self.load_harness(session_id, version_id)
            existing = [
                decode_versioned_record_json(CompactionRecord, line, label="compaction")
                for line in (directory / "compactions.jsonl").read_bytes().split(b"\n")
                if line.strip()
            ]
            compactions = tuple(retain_compactions(existing, removed_ids))
        prepared = prepare_session_commit(
            request,
            current,
            harness,
            event_sequence=event_sequence,
            truncation=truncation,
            restore=restore,
            compactions=compactions,
        )
        decision = FileCommitDecision(
            **prepared.model_dump(), message_offset=message_offset, event_offset=event_offset
        )
        self._validate_appends(directory, decision)
        for version in decision.versions:
            self.save_version(version)
        write_model_atomic(directory / "commit.json", decision)
        self.finish(session_id)
        return decision.result

    def finish(self, session_id: str) -> None:
        directory = self.directory(session_id)
        path = directory / "commit.json"
        if not path.exists():
            return
        decision = decode_versioned_record_json(
            FileCommitDecision, read_text_atomic(path), label=f"Session decision {session_id}"
        )
        result = decision.result
        if result.session.session_id != session_id:
            raise PersistenceError("Session decision has a foreign identity")
        # No callback or public store query runs while replaying a decision.
        # Replacing each journal suffix makes an interrupted append repeatable.
        for version in decision.versions:
            self.save_version(version)
        self.load_harness(session_id, result.session.harness_version)
        if result.messages or result.truncation is not None:
            self._write_suffix(
                directory / "messages.jsonl", decision.message_offset, result.messages
            )
        for run in decision.runs:
            if run.session_id != session_id:
                raise PersistenceError("Session decision contains a foreign Run")
            write_model_atomic(self._record_path(directory / "runs", run.run_id), run)
        if decision.compactions is not None:
            write_text_atomic(
                directory / "compactions.jsonl",
                "".join(f"{item.model_dump_json()}\n" for item in decision.compactions),
            )
        if decision.invalidate_context:
            (directory / "context.json").unlink(missing_ok=True)
        self.save_session_record(directory, result.session)
        if result.events:
            self._write_suffix(directory / "events.jsonl", decision.event_offset, result.events)
        self.save_result(result)
        path.unlink()

    def save_result(self, result: SessionCommitResult) -> None:
        write_model_atomic(
            self._result_path(result.session.session_id, result.operation_id), result
        )

    def save_version(self, version: HarnessVersion) -> None:
        path = self._version_path(version.session_id, version.version_id)
        if path.exists():
            if self.load_harness(version.session_id, version.version_id) != version:
                raise PersistenceError("immutable Harness checkpoint cannot be overwritten")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        write_model_atomic(path, version)

    @staticmethod
    def save_session_record(directory: Path, session: SessionRecord) -> None:
        # Controls are a read projection of the referenced checkpoint.
        write_text_atomic(directory / "session.json", session.model_dump_json(exclude={"controls"}))

    def _version_path(self, session_id: str, version_id: str) -> Path:
        return self._record_path(self.directory(session_id) / "harness", version_id)

    def _result_path(self, session_id: str, operation_id: str) -> Path:
        return self._record_path(self.directory(session_id) / "operations", operation_id)

    @staticmethod
    def _record_path(directory: Path, identity: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity) is None:
            raise ValueError("invalid Session record identity")
        return directory / f"{identity}.json"

    @staticmethod
    def _write_suffix(path: Path, offset: int, records: Sequence[OuroModel]) -> None:
        payload = "".join(f"{record.model_dump_json()}\n" for record in records).encode("utf-8")
        with path.open("r+b") as stream:
            if stream.seek(0, 2) < offset:
                raise PersistenceError("Session decision offset exceeds journal size")
            stream.seek(offset)
            stream.write(payload)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

    def _locate_cut(self, request: SessionCommit) -> tuple[MessageTruncation, int, set[str]]:
        removed: set[str] = set()
        first = None
        retained = None
        cut = 0
        with closing(
            reverse_jsonl(self.directory(request.before.session_id) / "messages.jsonl")
        ) as lines:
            for offset, line in lines:
                message = decode_versioned_record_json(
                    MessageRecord, line, label="history boundary"
                )
                if not removed and message.message_id != request.before.last_message_id:
                    raise SessionCommitConflict("Session message head changed")
                if first is not None:
                    retained = message
                    break
                removed.add(message.message_id)
                if message.message_id == request.before_message_id:
                    first, cut = message, offset
        if first is None:
            raise HistoryBoundaryUnavailable("message boundary not found")
        return (
            MessageTruncation(
                first_removed=first, last_retained=retained, removed_count=len(removed)
            ),
            cut,
            removed,
        )

    def _validate_appends(self, directory: Path, decision: FileCommitDecision) -> None:
        messages = decision.result.messages
        ids = [message.message_id for message in messages]
        if len(set(ids)) != len(ids):
            raise SessionCommitConflict("message batch contains duplicate identities")
        for run in decision.runs:
            self._record_path(directory / "runs", run.run_id)
        for identity in ids:
            if self.queries.message(directory / "messages.jsonl", identity) is not None:
                raise SessionCommitConflict("message identity is already committed")
