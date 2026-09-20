from __future__ import annotations

import json
import os
import re
import shutil
import struct
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, closing, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Self, TypeVar

from pydantic import ValidationError

from ouro.core.agents import DelegationRecord
from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import (
    DelegationRevisionConflictError,
    PersistenceError,
    ResourceNotFoundError,
    UnsupportedDataVersionError,
)
from ouro.core.events import MODEL_STREAM_DELTA_EVENT_TYPES, EventRecord
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.message_frames import MessageTruncation
from ouro.core.messages import MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.resources import SessionResourceReference, SessionResourceReferences
from ouro.core.session_commits import (
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
    SessionCommitStep,
)
from ouro.core.session_controls import SessionControlState
from ouro.core.session_extensions import (
    SessionExtensionStateRecord,
)
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_io import (
    read_text_atomic,
    replace_path_with_retry,
    write_json_atomic,
    write_model_atomic,
)
from ouro.local_locks import is_lock_held
from ouro.sessions.base import (
    RuntimeSessionSnapshot,
    SessionObservationState,
    SessionSnapshot,
)
from ouro.sessions.file_commits import FileSessionCommits
from ouro.sessions.index import SessionIndex
from ouro.sessions.layout import DATA_LAYOUT_VERSION, DataLayoutManifest
from ouro.sessions.local_index import LocalSessionIndex
from ouro.sessions.locks import SessionMutationLock, SessionRunLock
from ouro.sessions.message_index import MessageJournalQueries
from ouro.sessions.message_journal import reverse_jsonl
from ouro.sessions.portable import SessionBundle, populate_sessions

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_JournalRecord = TypeVar("_JournalRecord", MessageRecord, EventRecord)
_EVENT_SEQUENCE_INDEX_RECORD = struct.Struct(">QQ")
_EVENT_SEQUENCE_INDEX_STRIDE = 256


class LocalSessionStore:
    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.sessions_dir = self.root / "sessions"
        self.session_locks_dir = self.root / "locks" / "sessions"
        self.session_mutation_locks_dir = self.root / "locks" / "session-mutations"
        self.trash_sessions_dir = self.root / "trash" / "sessions"
        self._event_sequence_cache: dict[str, tuple[int, int]] = {}
        self._event_sequence_index_cache: dict[str, tuple[int, int] | None] = {}
        self._event_run_offset_cache: dict[tuple[str, str], int] = {}
        self._message_queries = MessageJournalQueries()
        self._commits = FileSessionCommits(self._session_dir, self._message_queries)
        self._delegation_paths: OrderedDict[str, Path] = OrderedDict()
        self._delegation_path_lock = Lock()
        self._layout_path = self.root / "layout.json"
        self._catalog = LocalSessionIndex(
            self.sessions_dir,
            self.session_locks_dir,
        )

    def initialize(self) -> None:
        self._initialize_layout()
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.session_locks_dir.mkdir(parents=True, exist_ok=True)
        self.session_mutation_locks_dir.mkdir(parents=True, exist_ok=True)
        self.trash_sessions_dir.mkdir(parents=True, exist_ok=True)
        self._recover_trash_staging()
        self._catalog.try_initialize()

    def create_session(self, session: SessionRecord) -> None:
        self.create_session_snapshot(session, [])

    def import_bundle(self, bundle: SessionBundle) -> None:
        with TemporaryDirectory(prefix=".session-import-", dir=self.root) as directory:
            staged = LocalSessionStore(directory)
            staged.initialize()
            populate_sessions(staged, bundle)
            identities = sorted(item.snapshot.session.session_id for item in bundle.sessions)
            published: list[str] = []
            with ExitStack() as locks:
                for identity in identities:
                    locks.enter_context(self.session_mutation(identity))
                    if self._session_dir(identity).exists():
                        raise PersistenceError(f"session already exists: {identity}")
                try:
                    for identity in identities:
                        staged._session_dir(identity).rename(self._session_dir(identity))
                        published.append(identity)
                except BaseException:
                    for identity in reversed(published):
                        self._session_dir(identity).rename(staged._session_dir(identity))
                    raise
            for identity in identities:
                self.refresh_catalog(identity)

    def create_delegation(self, delegation: DelegationRecord) -> DelegationRecord:
        root_id = delegation.root_session_id
        with self.session_mutation(root_id):
            self._require_session_dir(root_id)
            if (
                self.find_delegation_by_idempotency_key(root_id, delegation.idempotency_key)
                is not None
            ):
                raise PersistenceError(
                    f"delegation idempotency key already exists: {delegation.idempotency_key}"
                )
            path = self._delegation_path(root_id, delegation.delegation_id)
            if path.exists():
                raise PersistenceError(f"delegation already exists: {delegation.delegation_id}")
            self._write_model_atomic(path, delegation)
        return delegation

    def load_delegation(
        self,
        root_session_id: str,
        delegation_id: str,
    ) -> DelegationRecord:
        with self.session_mutation(root_session_id):
            path = self._delegation_path(root_session_id, delegation_id)
            try:
                return decode_versioned_record_json(
                    DelegationRecord,
                    read_text_atomic(path),
                    label=f"delegation {delegation_id}",
                )
            except FileNotFoundError:
                raise
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(f"could not load delegation {delegation_id}: {exc}") from exc

    def load_delegation_by_id(self, delegation_id: str) -> DelegationRecord:
        validated = _validated_record_id(delegation_id, kind="delegation")
        with self._delegation_path_lock:
            cached = self._delegation_paths.get(validated)
            if cached is not None:
                self._delegation_paths.move_to_end(validated)
        if cached is not None:
            return self.load_delegation(cached.parent.parent.name, validated)
        paths = list(self.sessions_dir.glob(f"*/delegations/{validated}.json"))
        if not paths:
            raise FileNotFoundError(delegation_id)
        if len(paths) != 1:
            raise PersistenceError(f"delegation identity is not globally unique: {delegation_id}")
        relation = self.load_delegation(paths[0].parent.parent.name, validated)
        with self._delegation_path_lock:
            self._delegation_paths[validated] = paths[0]
            if len(self._delegation_paths) > 256:
                self._delegation_paths.popitem(last=False)
        return relation

    def save_delegation(
        self,
        delegation: DelegationRecord,
        *,
        expected_revision: int,
    ) -> DelegationRecord:
        root_id = delegation.root_session_id
        with self.session_mutation(root_id):
            current = self.load_delegation(root_id, delegation.delegation_id)
            if current.revision != expected_revision:
                raise DelegationRevisionConflictError(
                    "delegation revision conflict: "
                    f"expected {expected_revision}, found {current.revision}"
                )
            if delegation.revision != expected_revision:
                raise DelegationRevisionConflictError(
                    "delegation revision conflict: supplied record does not match expected revision"
                )
            updated = DelegationRecord.model_validate(
                delegation.model_copy(
                    update={"revision": expected_revision + 1, "updated_at": utc_now()}
                ).model_dump()
            )
            self._write_model_atomic(
                self._delegation_path(root_id, delegation.delegation_id), updated
            )
            return updated

    def list_delegations(
        self,
        root_session_id: str,
        *,
        parent_session_id: str | None = None,
    ) -> list[DelegationRecord]:
        with self.session_mutation(root_session_id):
            root_dir = self._require_session_dir(root_session_id)
            records = self._load_delegations_from_dir(root_dir / "delegations")
        if parent_session_id is not None:
            records = [item for item in records if item.parent_session_id == parent_session_id]
        return sorted(records, key=lambda item: (item.created_at, item.delegation_id))

    def find_delegation_by_child_session(
        self,
        root_session_id: str,
        child_session_id: str,
    ) -> DelegationRecord | None:
        _validated_record_id(child_session_id, kind="session")
        for record in self.list_delegations(root_session_id):
            if record.child_session_id == child_session_id:
                return record
        return None

    def find_delegation_by_idempotency_key(
        self,
        root_session_id: str,
        idempotency_key: str,
    ) -> DelegationRecord | None:
        for record in self.list_delegations(root_session_id):
            if record.idempotency_key == idempotency_key:
                return record
        return None

    def create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None = None,
        harness_versions: list[HarnessVersion] | None = None,
        compactions: list[CompactionRecord] | None = None,
        commits: list[SessionCommitResult] | None = None,
    ) -> None:
        with self.session_mutation(session.session_id):
            self._create_session_snapshot(
                session,
                messages,
                resource_references,
                harness_versions,
                compactions,
                commits,
            )

    def _create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None,
        harness_versions: list[HarnessVersion] | None,
        compactions: list[CompactionRecord] | None,
        commits: list[SessionCommitResult] | None,
    ) -> None:
        session_dir = self._session_dir(session.session_id)
        if session_dir.exists():
            raise PersistenceError(f"session already exists: {session.session_id}")

        staging_dir = self.sessions_dir / f".{new_id('stage')}"
        try:
            (staging_dir / "runs").mkdir(parents=True)
            (staging_dir / "event-index" / "runs").mkdir(parents=True)
            (staging_dir / "resources").mkdir(parents=True)
            (staging_dir / "harness").mkdir(parents=True)
            versions = (
                harness_versions
                if harness_versions is not None
                else [
                    HarnessVersion(
                        session_id=session.session_id,
                        version_id=session.harness_version,
                        state=HarnessState(controls=session.controls),
                    )
                ]
            )
            checkpoints = {version.version_id: version for version in versions}
            if session.harness_version not in checkpoints or any(
                message.before_harness_version is not None
                and message.before_harness_version not in checkpoints
                for message in messages
            ):
                raise PersistenceError("Session snapshot has a missing Harness checkpoint")
            staged = FileSessionCommits(lambda _session_id: staging_dir, MessageJournalQueries())
            for version in versions:
                if version.session_id != session.session_id:
                    raise PersistenceError("Harness checkpoint belongs to another Session")
                staged.save_version(version)
            for receipt in commits or ():
                if receipt.session.session_id != session.session_id:
                    raise PersistenceError("Session receipt belongs to another Session")
                staged.save_result(receipt)
            session = session.model_copy(
                update={
                    "message_count": len(messages),
                    "last_message_id": messages[-1].message_id if messages else None,
                }
            )
            self._commits.save_session_record(staging_dir, session)
            for name in ("messages.jsonl", "events.jsonl", "compactions.jsonl"):
                (staging_dir / name).touch()
            for message in messages:
                if message.session_id != session.session_id:
                    raise PersistenceError(
                        f"message {message.message_id} does not belong to "
                        f"session {session.session_id}"
                    )
                self._append_jsonl(staging_dir / "messages.jsonl", message)
            compaction_records = compactions or []
            if any(item.session_id != session.session_id for item in compaction_records):
                raise PersistenceError(
                    f"compaction does not belong to session {session.session_id}"
                )
            if session.active_compaction_id is not None and not any(
                item.compaction_id == session.active_compaction_id for item in compaction_records
            ):
                raise PersistenceError(
                    f"active compaction {session.active_compaction_id} is missing from "
                    f"session snapshot {session.session_id}"
                )
            for compaction in compaction_records:
                self._append_jsonl(staging_dir / "compactions.jsonl", compaction)
            self._write_json_atomic(
                staging_dir / "resources" / "index.json",
                SessionResourceReferences(resources=resource_references or []).model_dump(
                    mode="json"
                ),
            )
            replace_path_with_retry(staging_dir, session_dir)
            self._event_sequence_cache[session.session_id] = (0, 0)
            self._event_sequence_index_cache[session.session_id] = None
        except (OSError, PersistenceError) as exc:
            with suppress(OSError):
                shutil.rmtree(staging_dir)
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"could not create session {session.session_id}: {exc}") from exc

    def save_session(self, session: SessionRecord) -> None:
        with self.session_mutation(session.session_id):
            current = self.load_session_record(session.session_id)
            if session.history_epoch != current.history_epoch:
                raise SessionCommitConflict("Session history changed; stale Session write rejected")
            state = self.load_harness(session.session_id).state
            steps: tuple[SessionCommitStep, ...] = ()
            if (
                session.harness_version == current.harness_version
                and session.controls != current.controls
            ):
                steps = (
                    SessionCommitStep(
                        harness=state.model_copy(update={"controls": session.controls})
                    ),
                )
            self.commit_session(SessionCommit(before=current, after=session, steps=steps))

    def trash_session_tree(
        self,
        root_session_id: str,
        session_ids: tuple[str, ...],
    ) -> str:
        _validated_record_id(root_session_id, kind="session")
        normalized = tuple(dict.fromkeys(session_ids))
        if not normalized:
            raise ValueError("session tree cannot be empty")
        with ExitStack() as stack:
            for session_id in sorted(normalized):
                _validated_record_id(session_id, kind="session")
                stack.enter_context(self.session_mutation(session_id))
            return self._trash_session_tree(root_session_id, normalized)

    def _trash_session_tree(
        self,
        root_session_id: str,
        session_ids: tuple[str, ...],
    ) -> str:
        for session_id in session_ids:
            self._require_session_dir(session_id)
        trash_id = new_id("trash")
        staging_dir = self.trash_sessions_dir / f".{trash_id}.staging"
        staged_sessions = staging_dir / "sessions"
        target_dir = self.trash_sessions_dir / trash_id
        moved: list[str] = []
        try:
            staged_sessions.mkdir(parents=True)
            self._write_json_atomic(
                staging_dir / "trash.json",
                {
                    "trash_id": trash_id,
                    "root_session_id": root_session_id,
                    "session_ids": list(session_ids),
                    "trashed_at": utc_now(),
                },
            )
            for session_id in session_ids:
                self._session_dir(session_id).replace(staged_sessions / session_id)
                moved.append(session_id)
            staging_dir.replace(target_dir)
        except (OSError, PersistenceError) as exc:
            if target_dir.exists() and all(
                not self._session_dir(session_id).exists() for session_id in session_ids
            ):
                return trash_id
            retained_at = target_dir if target_dir.exists() else staging_dir
            retained_sessions = retained_at / "sessions"
            rollback_errors: list[str] = []
            for session_id in reversed(moved):
                source = retained_sessions / session_id
                target = self._session_dir(session_id)
                if not source.exists() or target.exists():
                    continue
                try:
                    source.replace(target)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{session_id}: {rollback_exc}")
            if not rollback_errors:
                with suppress(OSError):
                    shutil.rmtree(retained_at)
            else:
                raise PersistenceError(
                    f"could not publish trash tree for root session {root_session_id}; "
                    f"data retained at {retained_at}: {'; '.join(rollback_errors)}"
                ) from exc
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(
                f"could not move session tree {root_session_id} to trash: {exc}"
            ) from exc
        return trash_id

    def append_message(self, message: MessageRecord) -> None:
        self.append_messages([message])

    def append_messages(self, messages: list[MessageRecord]) -> None:
        if not messages:
            return
        with self.session_mutation(messages[0].session_id):
            current = self.load_session_record(messages[0].session_id)
            result = self.commit_session(
                SessionCommit(
                    before=current,
                    steps=tuple(SessionCommitStep(message=item) for item in messages),
                )
            )
            for supplied, stored in zip(messages, result.messages, strict=True):
                supplied.sequence = stored.sequence
                supplied.before_harness_version = stored.before_harness_version

    def load_messages_tail(self, session_id: str, *, limit: int = 100) -> list[MessageRecord]:
        if limit < 1:
            raise ValueError("message tail limit must be positive")
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "messages.jsonl"
            messages: list[MessageRecord] = []
            with closing(reverse_jsonl(path)) as lines:
                for _, line in lines:
                    messages.append(
                        decode_versioned_record_json(
                            MessageRecord, line, label=f"message in session {session_id}"
                        )
                    )
                    if len(messages) == limit:
                        break
            return messages

    def truncate_messages(
        self, session_id: str, *, before_message_id: str, expected_last_message_id: str
    ) -> MessageTruncation:
        with self.session_mutation(session_id):
            current = self.load_session_record(session_id)
            if current.last_message_id != expected_last_message_id:
                raise SessionCommitConflict("session history changed; reload before truncating")
            result = self.commit_session(
                SessionCommit(
                    before=current,
                    before_message_id=before_message_id,
                )
            )
            assert result.truncation is not None
            return result.truncation

    def load_harness(self, session_id: str, version_id: str | None = None) -> HarnessVersion:
        with self.session_mutation(session_id):
            if version_id is None:
                return self._commits.load_session_harness(session_id)[1]
            return self._commits.load_harness(session_id, version_id)

    def load_commit(self, session_id: str, operation_id: str) -> SessionCommitResult | None:
        with self.session_mutation(session_id):
            self._require_session_dir(session_id)
            return self._commits.load_result(session_id, operation_id)

    def commit_session(self, request: SessionCommit) -> SessionCommitResult:
        with self.session_mutation(request.before.session_id):
            try:
                return self._commits.commit(request)
            except OSError as exc:
                raise PersistenceError(
                    f"could not commit Session {request.before.session_id}: {exc}"
                ) from exc

    def append_event(self, event: EventRecord) -> None:
        with self.session_mutation(event.session_id):
            session_dir = self._require_session_dir(event.session_id)
            path = session_dir / "events.jsonl"
            run_offset: int | None = None
            if event.run_id is not None:
                run_offset = self._event_run_offset(event.session_id, event.run_id, path)
            journal_size = path.stat().st_size if path.exists() else 0
            self._prepare_event_sequence_index(
                event.session_id,
                path,
                sequence=event.sequence,
            )
            self._append_jsonl(path, event)
            if (
                event.run_id is not None
                and run_offset not in {None, 0}
                and run_offset == journal_size
            ):
                self._write_event_run_offset(
                    event.session_id,
                    event.run_id,
                    run_offset,
                    journal_size=path.stat().st_size,
                    first_event=event,
                )
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
            run_offset: int | None = None
            if ordered.run_id is not None:
                run_offset = self._event_run_offset(event.session_id, ordered.run_id, path)
            journal_size = path.stat().st_size if path.exists() else 0
            self._prepare_event_sequence_index(
                event.session_id,
                path,
                sequence=sequence,
            )
            # Delta events are group-committed by the next durable boundary
            # event. This preserves ordered live replay without one fsync per token.
            self._append_jsonl(
                path,
                ordered,
                durable=ordered.event_type not in MODEL_STREAM_DELTA_EVENT_TYPES,
            )
            if (
                ordered.run_id is not None
                and run_offset not in {None, 0}
                and run_offset == journal_size
            ):
                self._write_event_run_offset(
                    event.session_id,
                    ordered.run_id,
                    run_offset,
                    journal_size=path.stat().st_size,
                    first_event=ordered,
                )
            self._event_sequence_cache[event.session_id] = (path.stat().st_size, sequence)
            return ordered

    def save_run(self, run: RunRecord) -> None:
        with self.session_mutation(run.session_id):
            session_dir = self._require_session_dir(run.session_id)
            run_dir = session_dir / "runs"
            run_dir.mkdir(parents=True, exist_ok=True)
            run_id = _validated_record_id(run.run_id, kind="run")
            run_path = run_dir / f"{run_id}.json"
            if not run_path.exists():
                journal = session_dir / "events.jsonl"
                offset_path = self._event_run_offset_path(run.session_id, run_id)
                if (run.session_id, run_id) in self._event_run_offset_cache or offset_path.exists():
                    self._event_run_offset(run.session_id, run_id, journal)
                else:
                    self._write_event_run_offset(
                        run.session_id,
                        run_id,
                        journal.stat().st_size if journal.exists() else 0,
                        journal_size=journal.stat().st_size if journal.exists() else 0,
                    )
            self._write_model_atomic(run_path, run)

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
                return decode_versioned_record_json(
                    ContextSnapshotRecord,
                    read_text_atomic(path),
                    label=f"latest context for session {session_id}",
                )
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
                    decode_versioned_record_json(
                        EventRecord,
                        line,
                        label=f"event in session {session_id}",
                    )
                    for line in self._iter_jsonl(session_dir / "events.jsonl")
                ]
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(f"could not load session {session_id}: {exc}") from exc
            snapshot = SessionSnapshot(
                **runtime.model_dump(),
                events=events,
                extension_states=self.load_session_extension_states(session_id),
                harness_versions=[
                    self._commits.load_harness(session_id, path.stem)
                    for path in sorted((session_dir / "harness").glob("*.json"))
                ],
                commits=[
                    decode_versioned_record_json(
                        SessionCommitResult,
                        read_text_atomic(path),
                        label=f"Session receipt {path.stem}",
                    )
                    for path in sorted((session_dir / "operations").glob("*.json"))
                ],
            )
        return snapshot

    def load_runtime_session(self, session_id: str) -> RuntimeSessionSnapshot:
        with self.session_mutation(session_id):
            return self._load_runtime_session(session_id)

    def load_session_observation(self, session_id: str) -> SessionObservationState:
        with self.session_mutation(session_id):
            runtime = self._load_runtime_session(session_id)
            active_run_id = runtime.session.active_run_id
            path = self._session_dir(session_id) / "events.jsonl"
            event_high_water = self._max_event_sequence(session_id, path)
            active_events = (
                self._read_events_for_run(
                    path,
                    session_id=session_id,
                    run_id=active_run_id,
                    offset=self._event_run_offset(session_id, active_run_id, path),
                )
                if active_run_id is not None
                else []
            )
            return SessionObservationState(
                **runtime.model_dump(),
                active_run_events=active_events,
                event_high_water=event_high_water,
            )

    def load_runs(self, session_id: str) -> list[RunRecord]:
        with self.session_mutation(session_id):
            return self._load_runs(session_id)

    def _load_runs(self, session_id: str) -> list[RunRecord]:
        session_dir = self._require_session_dir(session_id)
        try:
            runs = [
                decode_versioned_record_json(
                    RunRecord,
                    read_text_atomic(path),
                    label=f"run record {path}",
                )
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
                decode_versioned_record_json(
                    MessageRecord,
                    line,
                    label=f"message in session {session_id}",
                )
                for line in self._iter_jsonl(session_dir / "messages.jsonl")
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

    def load_compactions(self, session_id: str) -> list[CompactionRecord]:
        with self.session_mutation(session_id):
            session_dir = self._require_session_dir(session_id)
            return self._load_compactions(session_dir)

    def latest_event_sequence(self, session_id: str) -> int:
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "events.jsonl"
            return self._max_event_sequence(session_id, path)

    def load_session_record(self, session_id: str) -> SessionRecord:
        with self.session_mutation(session_id):
            return self._commits.load_session_harness(session_id)[0]

    def load_session_harness(self, session_id: str) -> tuple[SessionRecord, HarnessVersion]:
        with self.session_mutation(session_id):
            session, version = self._commits.load_session_harness(session_id)
            session.controls = SessionControlState.model_validate(session.controls.model_dump())
            return session, version

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

    def load_message(self, session_id: str, message_id: str) -> MessageRecord:
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "messages.jsonl"
            message = self._message_queries.message(path, message_id)
            if message is not None:
                return message
        raise PersistenceError(f"message not found in session {session_id}: {message_id}")

    def load_run_messages(self, session_id: str, run_id: str) -> list[MessageRecord]:
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "messages.jsonl"
            return self._message_queries.run(path, run_id)

    def find_queued_input_message(self, session_id: str, queue_id: str) -> MessageRecord | None:
        with self.session_mutation(session_id):
            path = self._require_session_dir(session_id) / "messages.jsonl"
            return self._message_queries.queued(path, queue_id)

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
            if run_id is not None and cursor == 0:
                cursor = self._event_run_offset(session_id, run_id, path)
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
        records: list[EventRecord] = []
        try:
            with self.session_mutation(session_id):
                path = self._require_session_dir(session_id) / "events.jsonl"
                if after_sequence >= self._max_event_sequence(session_id, path):
                    return [], False
                offset = self._event_offset_after_sequence(
                    session_id,
                    path,
                    after_sequence=after_sequence,
                )
                with path.open("rb") as file:
                    file.seek(offset)
                    for raw in file:
                        if not raw.strip():
                            continue
                        event = decode_versioned_record_json(
                            EventRecord,
                            raw,
                            label=f"event in session {session_id}",
                        )
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
                return decode_versioned_record_json(
                    RunRecord,
                    read_text_atomic(path),
                    label=f"run {run_id} for session {session_id}",
                )
            except FileNotFoundError:
                raise
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(
                    f"could not load run {run_id} for session {session_id}: {exc}"
                ) from exc

    def list_sessions(self) -> list[SessionRecord]:
        return [
            self.load_session_record(path.parent.name)
            for path in sorted(self.sessions_dir.glob("[!.]*/session.json"))
        ]

    def load_resource_references(self, session_id: str) -> list[SessionResourceReference]:
        with self.session_mutation(session_id):
            return self._load_resource_references(session_id)

    def _load_resource_references(self, session_id: str) -> list[SessionResourceReference]:
        path = self._resource_reference_path(session_id)
        try:
            return SessionResourceReferences.model_validate_json(read_text_atomic(path)).resources
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
                references = self.load_resource_references(path.parent.parent.name)
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(
                    f"could not load session resource references {path}: {exc}"
                ) from exc
            if any(reference.resource_id == resource_id for reference in references):
                return True
        return False

    def load_session_extension_states(self, session_id: str) -> list[SessionExtensionStateRecord]:
        return list(self.load_harness(session_id).state.states)

    def scan_catalog(self) -> SessionIndex:
        return self._catalog.scan()

    def rebuild_catalog(self) -> SessionIndex:
        return self._catalog.rebuild()

    def try_rebuild_catalog(self) -> str | None:
        return self._catalog.try_rebuild()

    def refresh_catalog(
        self,
        session_id: str,
    ) -> str | None:
        return self._catalog.try_update(session_id)

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
        return _SessionMutation(self, session_id)

    def is_session_locked(self, session_id: str) -> bool:
        try:
            return is_lock_held(self._lock_path(session_id))
        except OSError as exc:
            raise PersistenceError(
                f"could not inspect session lock for {session_id}: {exc}"
            ) from exc

    def _session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / _validated_record_id(session_id, kind="session")

    def _delegation_path(self, root_session_id: str, delegation_id: str) -> Path:
        root_dir = self._require_session_dir(root_session_id)
        validated = _validated_record_id(delegation_id, kind="delegation")
        return root_dir / "delegations" / f"{validated}.json"

    def _load_delegations_from_dir(self, directory: Path) -> list[DelegationRecord]:
        return [self._load_delegation_path(path) for path in sorted(directory.glob("*.json"))]

    @staticmethod
    def _load_delegation_path(path: Path) -> DelegationRecord:
        try:
            return decode_versioned_record_json(
                DelegationRecord,
                read_text_atomic(path),
                label=f"delegation {path}",
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(f"could not load delegation {path}: {exc}") from exc

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
            if self.sessions_dir.exists() and any(self.sessions_dir.iterdir()):
                raise UnsupportedDataVersionError(
                    f"OURO Session store has no format version: {self._layout_path}"
                )
            self._write_model_atomic(self._layout_path, DataLayoutManifest())
            return
        try:
            manifest = DataLayoutManifest.model_validate_json(read_text_atomic(self._layout_path))
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(
                f"could not load data layout manifest {self._layout_path}: {exc}"
            ) from exc
        version = (
            manifest.data_layout_version
            if "data_layout_version" in manifest.model_fields_set
            else "missing"
        )
        if version != DATA_LAYOUT_VERSION:
            raise UnsupportedDataVersionError(
                f"unsupported OURO data layout at {self._layout_path}: "
                f"found {version}, expected {DATA_LAYOUT_VERSION}; "
                "use a new OURO_HOME or remove obsolete development data"
            )

    def _recover_trash_staging(self) -> None:
        for staging_dir in self.trash_sessions_dir.glob(".*.staging"):
            tree_manifest = staging_dir / "trash.json"
            staged_sessions = staging_dir / "sessions"
            if tree_manifest.is_file() and staged_sessions.is_dir():
                self._recover_trash_tree_staging(staging_dir, tree_manifest, staged_sessions)

    def _recover_trash_tree_staging(
        self,
        staging_dir: Path,
        manifest_path: Path,
        staged_sessions: Path,
    ) -> None:
        try:
            raw = json.loads(read_text_atomic(manifest_path))
            session_ids = raw["session_ids"]
            if not isinstance(session_ids, list) or not session_ids:
                raise ValueError("trash tree manifest has no session ids")
            normalized = tuple(
                _validated_record_id(str(session_id), kind="session") for session_id in session_ids
            )
            for session_id in normalized:
                staged = staged_sessions / session_id
                if staged.exists():
                    continue
                source = self._session_dir(session_id)
                if not source.exists():
                    raise FileNotFoundError(
                        f"session {session_id} is absent from both active and staged storage"
                    )
                source.replace(staged)
            target_name = staging_dir.name.removeprefix(".").removesuffix(".staging")
            target_dir = self.trash_sessions_dir / target_name
            if target_dir.exists():
                raise FileExistsError(target_dir)
            staging_dir.replace(target_dir)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PersistenceError(
                f"could not recover staged trash tree {staging_dir}: {exc}"
            ) from exc

    def _lock_path(self, session_id: str) -> Path:
        validated = _validated_record_id(session_id, kind="session")
        return self.session_locks_dir / f"{validated}.lock"

    def _mutation_lock_path(self, session_id: str) -> Path:
        validated = _validated_record_id(session_id, kind="session")
        return self.session_mutation_locks_dir / f"{validated}.lock"

    def _event_run_offset(
        self,
        session_id: str,
        run_id: str,
        journal: Path,
    ) -> int:
        """Return the first possible byte for a Run without rescanning old Runs."""

        validated_run_id = _validated_record_id(run_id, kind="run")
        key = (session_id, validated_run_id)
        cached = self._event_run_offset_cache.get(key)
        if cached is not None:
            return cached
        journal_size = journal.stat().st_size if journal.exists() else 0
        index_path = self._event_run_offset_path(session_id, validated_run_id)
        if index_path.exists():
            try:
                raw = json.loads(read_text_atomic(index_path))
                offset = raw["offset"]
                if (
                    raw.get("schema_version") == 1
                    and raw.get("run_id") == validated_run_id
                    and isinstance(offset, int)
                    and not isinstance(offset, bool)
                    and _is_record_boundary(journal, offset, journal_size)
                    and self._event_run_offset_matches(
                        journal,
                        run_id=validated_run_id,
                        offset=offset,
                        journal_size=journal_size,
                        index=raw,
                    )
                ):
                    self._event_run_offset_cache[key] = offset
                    return offset
            except OSError, KeyError, TypeError, ValueError, json.JSONDecodeError:
                # This is a derived accelerator. Rebuild it from the canonical
                # append-only journal instead of making the Session unreadable.
                pass
        offset, first_event = self._find_event_run_offset(
            journal,
            session_id=session_id,
            run_id=run_id,
        )
        self._write_event_run_offset(
            session_id,
            validated_run_id,
            offset,
            journal_size=journal_size,
            first_event=first_event,
        )
        return offset

    def _find_event_run_offset(
        self,
        journal: Path,
        *,
        session_id: str,
        run_id: str,
    ) -> tuple[int, EventRecord | None]:
        run_id_pattern = re.compile(
            rb'"run_id"\s*:\s*' + re.escape(json.dumps(run_id, ensure_ascii=False).encode("utf-8"))
        )
        try:
            with journal.open("rb") as file:
                while True:
                    offset = file.tell()
                    raw = file.readline()
                    if not raw:
                        return offset, None
                    if not raw.strip() or run_id_pattern.search(raw) is None:
                        continue
                    event = decode_versioned_record_json(
                        EventRecord,
                        raw,
                        label=f"event in session {session_id}",
                    )
                    if event.run_id == run_id:
                        return offset, event
        except (OSError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise PersistenceError(
                f"could not locate Run {run_id} events in Session {session_id}: {exc}"
            ) from exc

    def _write_event_run_offset(
        self,
        session_id: str,
        run_id: str,
        offset: int,
        *,
        journal_size: int,
        first_event: EventRecord | None = None,
    ) -> None:
        validated_run_id = _validated_record_id(run_id, kind="run")
        payload = {
            "schema_version": 1,
            "run_id": validated_run_id,
            "offset": offset,
            "journal_size": journal_size,
        }
        if first_event is not None:
            payload.update(
                {
                    "first_event_id": first_event.event_id,
                    "first_event_sequence": first_event.sequence,
                }
            )
        self._event_run_offset_cache[(session_id, validated_run_id)] = offset
        path = self._event_run_offset_path(session_id, validated_run_id)
        if offset == 0 and not path.exists():
            # Absence already means "scan from the journal start". Persisting
            # a zero cursor would add I/O without narrowing any future read.
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError, TypeError, ValueError:
            # This index is disposable. The in-memory offset remains valid for
            # this Store; a reopened Store can rebuild it from events.jsonl.
            return

    @staticmethod
    def _event_run_offset_matches(
        journal: Path,
        *,
        run_id: str,
        offset: int,
        journal_size: int,
        index: dict[str, object],
    ) -> bool:
        if offset == 0:
            return True
        if offset == journal_size:
            return index.get("journal_size") == journal_size
        event_id = index.get("first_event_id")
        sequence = index.get("first_event_sequence")
        if (
            not isinstance(event_id, str)
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
        ):
            return False
        try:
            with journal.open("rb") as file:
                file.seek(offset)
                payload = json.loads(file.readline())
        except OSError, TypeError, ValueError, json.JSONDecodeError:
            return False
        return (
            isinstance(payload, dict)
            and payload.get("run_id") == run_id
            and payload.get("event_id") == event_id
            and payload.get("sequence") == sequence
        )

    def _event_run_offset_path(self, session_id: str, run_id: str) -> Path:
        return self._session_dir(session_id) / "event-index" / "runs" / f"{run_id}.json"

    def _read_events_for_run(
        self,
        journal: Path,
        *,
        session_id: str,
        run_id: str,
        offset: int,
    ) -> list[EventRecord]:
        run_id_pattern = re.compile(
            rb'"run_id"\s*:\s*' + re.escape(json.dumps(run_id, ensure_ascii=False).encode("utf-8"))
        )
        events: list[EventRecord] = []
        try:
            with journal.open("rb") as file:
                file.seek(offset)
                for raw in file:
                    if not raw.strip() or run_id_pattern.search(raw) is None:
                        continue
                    event = decode_versioned_record_json(
                        EventRecord,
                        raw,
                        label=f"event in session {session_id}",
                    )
                    if event.run_id == run_id:
                        events.append(event)
        except (OSError, UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise PersistenceError(
                f"could not read Run {run_id} events in Session {session_id}: {exc}"
            ) from exc
        return events

    def _prepare_event_sequence_index(
        self,
        session_id: str,
        journal: Path,
        *,
        sequence: int,
    ) -> None:
        """Checkpoint a sparse sequence-to-byte cursor before journal append."""

        if session_id not in self._event_sequence_index_cache:
            self._event_sequence_index_cache[session_id] = self._load_last_sequence_checkpoint(
                session_id,
                journal,
            )
        previous = self._event_sequence_index_cache[session_id]
        if previous is not None and sequence - previous[0] < _EVENT_SEQUENCE_INDEX_STRIDE:
            return
        journal_size = journal.stat().st_size if journal.exists() else 0
        if previous is None and journal_size == 0:
            # Offset zero is the implicit checkpoint. Materialize the sparse
            # index only once it can skip a non-empty historical prefix.
            self._event_sequence_index_cache[session_id] = (sequence, journal_size)
            return
        index_path = self._event_sequence_index_path(session_id)
        try:
            index_path.parent.mkdir(parents=True, exist_ok=True)
            with index_path.open("wb" if previous is None else "ab") as file:
                file.write(_EVENT_SEQUENCE_INDEX_RECORD.pack(sequence, journal_size))
        except OSError, struct.error:
            # Checkpoints are query accelerators, not part of event durability.
            # Retry at the next sparse boundary without blocking this append.
            self._event_sequence_index_cache[session_id] = (sequence, journal_size)
            return
        self._event_sequence_index_cache[session_id] = (sequence, journal_size)

    def _load_last_sequence_checkpoint(
        self,
        session_id: str,
        journal: Path,
    ) -> tuple[int, int] | None:
        index_path = self._event_sequence_index_path(session_id)
        if not index_path.exists():
            return None
        try:
            index_size = index_path.stat().st_size
            complete_size = index_size - (index_size % _EVENT_SEQUENCE_INDEX_RECORD.size)
            if complete_size == 0:
                return None
            if complete_size != index_size:
                with index_path.open("r+b") as file:
                    file.truncate(complete_size)
            with index_path.open("rb") as file:
                file.seek(complete_size - _EVENT_SEQUENCE_INDEX_RECORD.size)
                sequence, offset = _EVENT_SEQUENCE_INDEX_RECORD.unpack(file.read())
            journal_size = journal.stat().st_size if journal.exists() else 0
            if not _is_record_boundary(journal, offset, journal_size):
                return None
            return sequence, offset
        except OSError, struct.error:
            return None

    def _event_offset_after_sequence(
        self,
        session_id: str,
        journal: Path,
        *,
        after_sequence: int,
    ) -> int:
        """Find a bounded forward-scan start using fixed-width sparse checkpoints."""

        index_path = self._event_sequence_index_path(session_id)
        try:
            record_size = _EVENT_SEQUENCE_INDEX_RECORD.size
            record_count = index_path.stat().st_size // record_size
            if record_count == 0:
                return 0
            target = after_sequence + 1
            low = 0
            high = record_count
            candidate: tuple[int, int] | None = None
            with index_path.open("rb") as index:
                while low < high:
                    middle = (low + high) // 2
                    index.seek(middle * record_size)
                    raw = index.read(record_size)
                    if len(raw) != record_size:
                        return 0
                    sequence, offset = _EVENT_SEQUENCE_INDEX_RECORD.unpack(raw)
                    if sequence <= target:
                        candidate = (sequence, offset)
                        low = middle + 1
                    else:
                        high = middle
            if candidate is None:
                return 0
            sequence, offset = candidate
            journal_size = journal.stat().st_size
            if not _is_record_boundary(journal, offset, journal_size) or offset == journal_size:
                return 0
            with journal.open("rb") as file:
                file.seek(offset)
                raw = file.readline()
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("sequence") != sequence:
                return 0
            return offset
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            struct.error,
        ):
            # A corrupt/missing accelerator must never hide canonical events.
            return 0

    def _event_sequence_index_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "event-index" / "sequences.bin"

    def _max_event_sequence(self, session_id: str, path: Path) -> int:
        size = path.stat().st_size if path.exists() else 0
        cached = self._event_sequence_cache.get(session_id)
        if cached is not None and cached[0] == size:
            return cached[1]
        if size == 0:
            self._event_sequence_cache[session_id] = (0, 0)
            return 0
        try:
            # A trailing LF only ends a record; its beginning may be many
            # blocks earlier. Reuse the complete-record reader used by messages.
            with closing(reverse_jsonl(path, block_size=8192)) as records:
                _, line = next(records, (0, b""))
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
            decode_versioned_record_json(
                CompactionRecord,
                line,
                label=f"compaction in {session_dir}",
            )
            for line in self._iter_jsonl(session_dir / "compactions.jsonl")
        ]

    def _write_model_atomic(self, path: Path, model: OuroModel) -> None:
        write_model_atomic(path, model)

    def _write_json_atomic(self, path: Path, value: object) -> None:
        write_json_atomic(path, value)

    def _append_jsonl(self, path: Path, model: OuroModel, *, durable: bool = True) -> None:
        self._append_jsonl_batch(path, [model], durable=durable)

    def _append_jsonl_batch(
        self,
        path: Path,
        models: Sequence[OuroModel],
        *,
        durable: bool = True,
    ) -> None:
        try:
            payload = "".join(f"{model.model_dump_json()}\n" for model in models)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                if durable:
                    os.fsync(file.fileno())
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(f"could not append batch to {path}: {exc}") from exc

    def _read_jsonl(self, path: Path) -> list[str]:
        return list(self._iter_jsonl(path))

    def _iter_jsonl(self, path: Path) -> Iterator[str]:
        if not path.exists():
            return
        try:
            # JSON Lines defines LF as the record delimiter. ``str.splitlines``
            # also treats valid JSON string content such as U+2028/U+2029 as a
            # boundary and would split one durable record in the middle.
            with path.open("r", encoding="utf-8", newline="") as file:
                for line in file:
                    if line.strip():
                        yield line
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
        run_id_pattern = (
            re.compile(
                rb'"run_id"\s*:\s*'
                + re.escape(json.dumps(run_id, ensure_ascii=False).encode("utf-8"))
            )
            if run_id is not None
            else None
        )
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
                    # Filtering belongs to the persistence query boundary. Avoid
                    # decoding unrelated records, then validate candidate matches
                    # through the canonical versioned-record decoder below.
                    if run_id_pattern is not None and run_id_pattern.search(raw) is None:
                        continue
                    record = decode_versioned_record_json(
                        model_type,
                        raw,
                        label=f"{model_type.__name__} in {path}",
                    )
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


def _is_record_boundary(path: Path, offset: int, size: int) -> bool:
    if offset < 0 or offset > size:
        return False
    if offset == 0:
        return True
    try:
        with path.open("rb") as file:
            file.seek(offset - 1)
            return file.read(1) == b"\n"
    except OSError:
        return False


class _SessionMutation(SessionMutationLock):
    def __init__(self, store: LocalSessionStore, session_id: str) -> None:
        super().__init__(store._mutation_lock_path(session_id), session_id=session_id)
        self._store = store

    def __enter__(self) -> Self:
        super().__enter__()
        try:
            self._store._commits.finish(self.session_id)
        except OSError as exc:
            self.release()
            raise PersistenceError(f"could not recover Session {self.session_id}: {exc}") from exc
        except BaseException:
            self.release()
            raise
        return self
