from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections import OrderedDict
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager, suppress
from datetime import datetime
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, local
from types import TracebackType
from typing import Self

from pydantic import ValidationError

from ouro.core.agents import DelegationRecord
from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.compaction import CompactionRecord, validate_compaction_snapshot
from ouro.core.content import media_resource_ids
from ouro.core.context_selection import validate_media_selection
from ouro.core.context_snapshots import (
    ContextSnapshotRecord,
    ModelRequestHeader,
    RequestManifestRecord,
    validate_request_snapshot,
)
from ouro.core.errors import (
    DelegationRevisionConflictError,
    PersistenceError,
    SessionRecoveryRequiredError,
    UnsupportedDataVersionError,
)
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.inputs import InputRecord, InputState, validate_input_snapshot
from ouro.core.message_content import validate_tool_message_snapshot
from ouro.core.message_frames import MessageTruncation
from ouro.core.message_statistics import RunMessageCounts, RunResultReferences
from ouro.core.messages import MessageFact, MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.provider import ProviderRetryNotice
from ouro.core.resources import ResourceRecord, SessionResourceReference
from ouro.core.response_drafts import ResponseDraftChunk, ResponseDraftPage
from ouro.core.session_commits import (
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
    SessionCommitStep,
)
from ouro.core.session_extensions import (
    SessionExtensionStateRecord,
)
from ouro.core.state import RunRecord, RunRequestCounts, SessionRecord
from ouro.core.tool_batches import ToolCallState, validate_call_states
from ouro.core.tool_facts import (
    ToolCallFacts,
    ToolCallIdentity,
    ToolCallRecord,
    ToolCallStart,
    ToolOutcomeRecord,
)
from ouro.local_io import (
    filesystem_path,
    read_text_atomic,
    replace_path_with_retry,
    write_json_atomic,
    write_model_atomic,
)
from ouro.local_locks import is_lock_held
from ouro.resources.files import (
    read_resource_content,
    require_published_resource,
    verify_resource_files,
    write_resource_contents,
)
from ouro.resources.media import (
    capture_outcome_media,
    materialize_context_media,
    materialize_outcome_media,
    validate_media_references,
)
from ouro.resources.prepared import validate_prepared_resources
from ouro.sessions import sqlite_facts, sqlite_resources
from ouro.sessions.backends.files.commits import FileSessionCommits
from ouro.sessions.backends.files.compactions import (
    read_compaction,
    read_compactions,
)
from ouro.sessions.backends.files.contexts import input_path as request_input_path
from ouro.sessions.backends.files.contexts import (
    read_request,
)
from ouro.sessions.backends.files.database import connect_data, initialize_index
from ouro.sessions.backends.files.delegations import (
    find_record as find_delegation,
)
from ouro.sessions.backends.files.delegations import (
    lookup_path as delegation_lookup_path,
)
from ouro.sessions.backends.files.delegations import (
    read_record as read_delegation,
)
from ouro.sessions.backends.files.delegations import (
    record_path as delegation_path,
)
from ouro.sessions.backends.files.index import LocalSessionIndex
from ouro.sessions.backends.files.inputs import input_path, read_input
from ouro.sessions.backends.files.journal import reverse_jsonl
from ouro.sessions.backends.files.messages import (
    FileMessageRecords,
)
from ouro.sessions.base import (
    RuntimeSessionSnapshot,
    SessionHistorySnapshot,
    SessionSnapshot,
)
from ouro.sessions.index import SessionIndex
from ouro.sessions.layout import DATA_LAYOUT_VERSION, DataLayoutManifest
from ouro.sessions.locks import SessionMutationLock, SessionRunLock
from ouro.sessions.observation import FactCursor, SessionFactPage
from ouro.sessions.portable import SessionBundle, populate_sessions
from ouro.sessions.sqlite_compactions import insert_compaction
from ouro.sessions.sqlite_connections import ExecutionConnections, NativeSessionRunLock
from ouro.sessions.sqlite_drafts import append_chunk as append_draft_chunk
from ouro.sessions.sqlite_drafts import publish_draft
from ouro.sessions.sqlite_drafts import read_page as read_draft_page
from ouro.sessions.sqlite_facts import load_snapshot as load_tool_facts
from ouro.sessions.sqlite_facts import load_states, read_call_state, write_call_state, write_facts
from ouro.sessions.sqlite_inputs import read_state, write_state
from ouro.sessions.sqlite_observation import advance_revision, read_changes, read_revision
from ouro.sessions.sqlite_requests import (
    read_headers,
    read_latest_header,
    read_request_header,
    write_header,
)
from ouro.sessions.sqlite_runs import read_pending_runs, read_request_counts, read_run, read_runs
from ouro.sessions.sqlite_sessions import read_session, require_session, write_session

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class LocalSessionStore:
    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.sessions_dir = self.root / "sessions"
        self.session_locks_dir = self.root / "locks" / "sessions"
        self.session_mutation_locks_dir = self.root / "locks" / "session-mutations"
        self.trash_sessions_dir = self.root / "trash" / "sessions"
        self._session_directory_ids: dict[str, tuple[int, int]] = {}
        self._messages = FileMessageRecords()
        self._connections: dict[str, ExecutionConnections] = {}
        self._commits = FileSessionCommits(self._session_dir, self._connections)
        self._read_state = local()
        self._delegation_paths: OrderedDict[str, Path] = OrderedDict()
        self._delegation_path_lock = Lock()
        self._layout_path = self.root / "layout.json"
        self._catalog = LocalSessionIndex(
            self.sessions_dir,
            self.session_locks_dir,
            self._connections,
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

    def load_tool_call(self, session_id: str, identity: ToolCallIdentity) -> ToolCallRecord | None:
        with self.session_read(session_id) as reader:
            return sqlite_facts.read_fact(reader.connection(), session_id, identity, ToolCallRecord)

    def load_tool_call_start(
        self, session_id: str, identity: ToolCallIdentity, attempt: int
    ) -> ToolCallStart | None:
        with self.session_read(session_id) as reader:
            return sqlite_facts.read_fact(
                reader.connection(),
                session_id,
                identity,
                ToolCallStart,
                attempt=attempt,
            )

    def load_tool_outcome(
        self, session_id: str, identity: ToolCallIdentity, attempt: int
    ) -> ToolOutcomeRecord | None:
        with self.session_read(session_id) as reader:
            outcome = sqlite_facts.read_fact(
                reader.connection(),
                session_id,
                identity,
                ToolOutcomeRecord,
                attempt=attempt,
            )
            return materialize_outcome_media(
                outcome,
                lambda resource_id: self._messages.media(
                    reader.connection(), self._require_session_dir(session_id), resource_id
                ),
            )

    def _capture_tool_outcome(
        self, outcome: ToolOutcomeRecord, resources: list[SessionResourceReference]
    ) -> ToolOutcomeRecord:
        directory = self._require_session_dir(outcome.session_id)
        with self.session_read(outcome.session_id) as reader:
            connection = reader.connection()
            for identity in media_resource_ids(outcome.value.content_blocks):
                require_published_resource(
                    directory / "resources" / "files",
                    sqlite_resources.read_reference(connection, outcome.session_id, identity),
                )
            previous = sqlite_facts.read_fact(
                connection,
                outcome.session_id,
                outcome.identity,
                ToolOutcomeRecord,
                attempt=outcome.attempt,
            )
            if previous == outcome:
                return outcome

            def read(identity: str) -> bytes:
                return self._messages.media(connection, directory, identity)

            def publish(record: SessionResourceReference, content: bytes) -> None:
                write_resource_contents(directory / "resources" / "files", ((record, content),))
                resources.append(record)

            value = materialize_outcome_media(outcome, read)
            assert value is not None
            return capture_outcome_media(value, materialize_outcome_media(previous, read), publish)

    def tool_outcomes_use_resource(self, session_id: str, resource_id: str) -> bool:
        with self.session_read(session_id) as scope:
            for record in sqlite_facts.read_facts(
                scope.connection(), session_id, ToolOutcomeRecord
            ):
                if resource_id in record.value.referenced_resource_ids:
                    return True
            return False

    def load_tool_call_state(
        self, session_id: str, identity: ToolCallIdentity
    ) -> ToolCallState | None:
        with self.session_read(session_id) as reader:
            return read_call_state(reader.connection(), session_id, identity)

    def load_input(self, session_id: str, input_id: str) -> InputRecord | None:
        with self.session_read(session_id):
            return read_input(self._require_session_dir(session_id), session_id, input_id)

    def load_input_state(self, session_id: str, input_id: str) -> InputState | None:
        with self.session_read(session_id) as reader:
            return read_state(reader.connection(), session_id, input_id)

    def import_bundle(
        self,
        bundle: SessionBundle,
        *,
        replace_session_ids: tuple[str, ...] = (),
        resource_contents: Mapping[str, Mapping[str, bytes]] | None = None,
    ) -> None:
        temporary = TemporaryDirectory(
            prefix=".session-import-", dir=filesystem_path(self.root), delete=False
        )
        staging = Path(temporary.name)
        retain_backups = False
        try:
            staged = LocalSessionStore(staging)
            staged.initialize()
            populate_sessions(staged, bundle, resource_contents=resource_contents)
            identities = sorted(item.snapshot.session.session_id for item in bundle.sessions)
            affected = sorted(set(identities) | set(replace_session_ids))
            published: list[str] = []
            backed_up: list[str] = []
            backups = staging / "backups"
            backups.mkdir()
            with ExitStack() as locks:
                for identity in affected:
                    locks.enter_context(self.session_mutation(identity))
                    if connections := self._connections.get(identity):
                        connections.close_idle()
                    if self._session_dir(identity).exists() and identity not in replace_session_ids:
                        raise PersistenceError(f"session already exists: {identity}")
                try:
                    for identity in replace_session_ids:
                        self._session_dir(identity).rename(backups / identity)
                        backed_up.append(identity)
                    for identity in identities:
                        staged._session_dir(identity).rename(self._session_dir(identity))
                        published.append(identity)
                except BaseException:
                    retain_backups = True
                    try:
                        for identity in reversed(published):
                            self._session_dir(identity).rename(staged._session_dir(identity))
                        for identity in reversed(backed_up):
                            (backups / identity).rename(self._session_dir(identity))
                    except OSError as exc:
                        raise PersistenceError(
                            f"Session import rollback failed; original data retained at {staging}"
                        ) from exc
                    retain_backups = False
                    raise
                finally:
                    for identity in affected:
                        self._invalidate_session_caches(identity)
            for identity in affected:
                self.refresh_catalog(identity)
        finally:
            if not retain_backups:
                temporary.cleanup()

    def _invalidate_session_caches(self, session_id: str) -> None:
        with self._delegation_path_lock:
            self._delegation_paths.clear()

    def _observe_session_directory(self, session_id: str) -> None:
        try:
            stat = self._session_dir(session_id).stat()
        except FileNotFoundError:
            return
        identity = (stat.st_dev, stat.st_ino)
        previous = self._session_directory_ids.get(session_id)
        if previous is not None and previous != identity:
            # Another Host can replace a Session without changing journal sizes.
            self._invalidate_session_caches(session_id)
        self._session_directory_ids[session_id] = identity

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
            path = delegation_path(self._require_session_dir(root_id), delegation.delegation_id)
            if path.exists():
                raise PersistenceError(f"delegation already exists: {delegation.delegation_id}")
            self._commits.commit_delegation(delegation)
        return delegation

    def load_delegation(
        self,
        root_session_id: str,
        delegation_id: str,
    ) -> DelegationRecord:
        with self.session_read(root_session_id):
            try:
                return read_delegation(
                    self._require_session_dir(root_session_id), root_session_id, delegation_id
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
            updated.require_frozen_contract(current)
            self._commits.commit_delegation(updated, previous=current)
            return updated

    def list_delegations(
        self,
        root_session_id: str,
        *,
        parent_session_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> list[DelegationRecord]:
        with self.session_read(root_session_id):
            root_dir = self._require_session_dir(root_session_id)
            if parent_run_id is None:
                records = [
                    read_delegation(root_dir, root_session_id, path.stem)
                    for path in (root_dir / "delegations").glob("*.json")
                ]
            else:
                records = [
                    read_delegation(root_dir, root_session_id, path.name)
                    for path in delegation_lookup_path(root_dir, "parents", parent_run_id).glob("*")
                ]
                if any(record.parent_run_id != parent_run_id for record in records):
                    raise PersistenceError("Delegation parent index disagrees with its record")
        if parent_session_id is not None:
            records = [item for item in records if item.parent_session_id == parent_session_id]
        return sorted(records, key=lambda item: (item.created_at, item.delegation_id))

    def find_delegation_by_child_session(
        self,
        root_session_id: str,
        child_session_id: str,
    ) -> DelegationRecord | None:
        with self.session_read(root_session_id):
            return find_delegation(
                self._require_session_dir(root_session_id),
                root_session_id,
                "children",
                child_session_id,
            )

    def find_delegation_by_idempotency_key(
        self,
        root_session_id: str,
        idempotency_key: str,
    ) -> DelegationRecord | None:
        with self.session_read(root_session_id):
            return find_delegation(
                self._require_session_dir(root_session_id),
                root_session_id,
                "requests",
                idempotency_key,
            )

    def create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None = None,
        harness_versions: list[HarnessVersion] | None = None,
        compactions: list[CompactionRecord] | None = None,
        resource_contents: Iterable[tuple[ResourceRecord, bytes]] = (),
        *,
        prepared_resources: bool = False,
        inputs: tuple[InputRecord, ...] = (),
        input_states: tuple[InputState, ...] | None = None,
        tool_facts: ToolCallFacts | None = None,
        call_states: tuple[ToolCallState, ...] = (),
        request_headers: tuple[ModelRequestHeader, ...] = (),
        latest_manifest: RequestManifestRecord | None = None,
    ) -> None:
        with self.session_mutation(session.session_id):
            self._create_session_snapshot(
                session,
                messages,
                resource_references,
                harness_versions,
                compactions,
                resource_contents,
                prepared_resources=prepared_resources,
                inputs=inputs,
                input_states=input_states,
                tool_facts=tool_facts,
                call_states=call_states,
                request_headers=request_headers,
                latest_manifest=latest_manifest,
            )

    def _create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None,
        harness_versions: list[HarnessVersion] | None,
        compactions: list[CompactionRecord] | None,
        resource_contents: Iterable[tuple[ResourceRecord, bytes]],
        *,
        prepared_resources: bool = False,
        inputs: tuple[InputRecord, ...] = (),
        input_states: tuple[InputState, ...] | None = None,
        tool_facts: ToolCallFacts | None = None,
        call_states: tuple[ToolCallState, ...] = (),
        request_headers: tuple[ModelRequestHeader, ...] = (),
        latest_manifest: RequestManifestRecord | None = None,
    ) -> None:
        messages = [
            item if item.sequence == index else item.model_copy(update={"sequence": index})
            for index, item in enumerate(messages, start=1)
        ]
        validate_request_snapshot(session.session_id, request_headers, latest_manifest)
        validate_media_references(
            messages,
            latest_manifest,
            (record.resource_id for record in resource_references or []),
            (tool_facts or ToolCallFacts()).outcomes,
        )
        compaction_records = compactions or []
        validate_compaction_snapshot(session, messages, compaction_records)
        if (boundary := session.context_selection.media_elided_through) is not None:
            validate_media_selection(
                session.context_selection,
                session.session_id,
                next((item for item in messages if item.message_id == boundary.message_id), None),
            )
        states = validate_input_snapshot(
            session.session_id, session.pending_input_ids, inputs, input_states, messages=messages
        )
        if tool_facts is not None:
            tool_facts.validate_snapshot(session.session_id)
        validate_call_states(
            call_states, tool_facts or ToolCallFacts(), session_id=session.session_id
        )
        validate_tool_message_snapshot(messages, tool_facts or ToolCallFacts())
        session_dir = self._session_dir(session.session_id)
        database = session_dir / "session-data.sqlite3"
        published = False
        if database.exists():
            with connect_data(session_dir) as connection:
                published = connection.execute("SELECT 1 FROM sessions").fetchone() is not None
        if published or (session_dir.exists() and not prepared_resources):
            raise PersistenceError(f"session already exists: {session.session_id}")

        if prepared_resources:
            validate_prepared_resources(self.root, session.session_id, resource_references or [])
        staging_dir = (
            session_dir if prepared_resources else self.sessions_dir / f".{new_id('stage')}"
        )
        try:
            if prepared_resources:
                # No Session owns this metadata until its native head is published. A retry
                # replaces the unaccepted metadata snapshot.
                for name in (
                    "harness",
                    "inputs",
                ):
                    path = staging_dir / name
                    if path.exists():
                        shutil.rmtree(path)
            (staging_dir / "harness").mkdir(parents=True, exist_ok=prepared_resources)
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
            staged = FileSessionCommits(lambda _session_id: staging_dir)
            for record in inputs:
                write_model_atomic(input_path(staging_dir, record.item.queue_id), record)
            for version in versions:
                if version.session_id != session.session_id:
                    raise PersistenceError("Harness checkpoint belongs to another Session")
                staged.save_version(version)
            session = session.model_copy(
                update={
                    "message_count": len(messages),
                    "controls": checkpoints[session.harness_version].state.controls,
                    "run_count": 0,
                    "resource_count": len({item.resource_id for item in resource_references or ()}),
                    "last_message_id": messages[-1].message_id if messages else None,
                }
            )
            for message in messages:
                if message.session_id != session.session_id:
                    raise PersistenceError(
                        f"message {message.message_id} does not belong to "
                        f"session {session.session_id}"
                    )
            if len({message.message_id for message in messages}) != len(messages):
                raise PersistenceError("Session snapshot contains duplicate message identities")
            write_resource_contents(staging_dir / "resources" / "files", resource_contents)
            if not prepared_resources:
                verify_resource_files(
                    staging_dir / "resources" / "files", resource_references or ()
                )
            if latest_manifest is not None:
                content_path = request_input_path(staging_dir, latest_manifest.context_id)
                content_path.parent.mkdir(exist_ok=True)
                write_model_atomic(content_path, latest_manifest)
            # All native records publish together, including an in-place resource
            # reservation. The journal remains private until this publication.
            with connect_data(staging_dir, mode="rwc") as connection:
                initialize_index(connection)
                self._messages.create(connection, staging_dir, messages)
                write_session(connection, session)
                for state in states:
                    write_state(connection, state)
                for reference in resource_references or ():
                    sqlite_resources.insert_reference(connection, session.session_id, reference)
                write_facts(connection, tool_facts.records if tool_facts is not None else ())
                for call_state in call_states:
                    write_call_state(connection, call_state)
                for compaction in compaction_records:
                    insert_compaction(connection, compaction)
                for header in request_headers:
                    write_header(connection, header)
                if latest_manifest is not None:
                    connection.execute(
                        "INSERT INTO latest_contexts VALUES (?, ?)",
                        (session.session_id, latest_manifest.context_id),
                    )
                    publish_draft(
                        connection,
                        next(
                            header
                            for header in request_headers
                            if header.context_id == latest_manifest.context_id
                        ),
                    )
            if not prepared_resources:
                replace_path_with_retry(staging_dir, session_dir)
        except (OSError, PersistenceError) as exc:
            if not prepared_resources:
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
            # Explicit trash also accepts an unreadable canonical Session.
            database = self._session_dir(session_id) / "session-data.sqlite3"
            if not database.is_file():
                raise FileNotFoundError(database)
            if connections := self._connections.get(session_id):
                connections.close_idle()
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

    def load_messages_tail(
        self, session_id: str, *, limit: int = 100, include_media: bool = True
    ) -> list[MessageRecord]:
        if limit < 1:
            raise ValueError("message tail limit must be positive")
        with self.session_read(session_id) as scope:
            directory = self._require_session_dir(session_id)
            path = directory / "messages.jsonl"
            with closing(reverse_jsonl(path)) as lines:
                return self._messages.materialize_many(
                    directory,
                    (
                        self._messages.entry(line, session_id=session_id)
                        for _, line in islice(lines, limit)
                    ),
                    connection=scope.connection(),
                    include_media=include_media,
                )

    def iter_messages_before(
        self, session_id: str, *, before: int | None = None
    ) -> Generator[tuple[int, MessageRecord]]:
        if before is not None and before < 0:
            raise ValueError("message cursor must be nonnegative")
        with self.session_read(session_id) as scope:
            directory = self._require_session_dir(session_id)
            path = directory / "messages.jsonl"
            with closing(reverse_jsonl(path, before=before)) as lines:
                for offset, line in lines:
                    yield (
                        offset,
                        self._messages.materialize(
                            directory,
                            self._messages.entry(line, session_id=session_id),
                            connection=scope.connection(),
                        ),
                    )

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
        with self.session_read(session_id):
            if version_id is None:
                return self.load_session_harness(session_id)[1]
            return self._commits.load_harness(session_id, version_id)

    def commit_session(self, request: SessionCommit) -> SessionCommitResult:
        with self.session_mutation(request.before.session_id):
            try:
                resources: list[SessionResourceReference] = []
                if request.tool_facts.outcomes:
                    request = request.model_copy(
                        update={
                            "tool_facts": request.tool_facts.model_copy(
                                update={
                                    "outcomes": tuple(
                                        self._capture_tool_outcome(outcome, resources)
                                        for outcome in request.tool_facts.outcomes
                                    )
                                }
                            )
                        }
                    )
                captured = {record.resource_id: record for record in resources}
                for identity in request.media_resource_ids:
                    require_published_resource(
                        self._require_session_dir(request.before.session_id)
                        / "resources"
                        / "files",
                        captured[identity]
                        if identity in captured
                        else self.load_resource_reference(request.before.session_id, identity),
                    )
                return self._commits.commit(request, tuple(resources))
            except OSError as exc:
                raise PersistenceError(
                    f"could not commit Session {request.before.session_id}: {exc}"
                ) from exc

    def commit_tool_outcome(self, outcome: ToolOutcomeRecord) -> ToolOutcomeRecord:
        with self.session_mutation(outcome.session_id):
            self._require_session_dir(outcome.session_id)
            try:
                resources: list[SessionResourceReference] = []
                outcome = self._capture_tool_outcome(outcome, resources)
                saved = self._commits.commit_outcome(outcome, tuple(resources))
                value = materialize_outcome_media(
                    saved,
                    lambda identity: read_resource_content(
                        self._require_session_dir(outcome.session_id) / "resources" / "files",
                        self.load_resource_reference(outcome.session_id, identity),
                    ),
                )
                assert value is not None
                return value
            except OSError as exc:
                raise PersistenceError(f"could not save tool Outcome: {exc}") from exc

    def save_run(self, run: RunRecord) -> None:
        with self.session_mutation(run.session_id):
            self._commits.commit_run(run, session=self.load_session_record(run.session_id))

    def append_response_draft(
        self, session_id: str, context_id: str, chunk: ResponseDraftChunk
    ) -> int:
        directory = self._session_dir(session_id)
        if (directory / "commit.json").exists():
            with self.session_mutation(session_id):
                pass
        connections = self._connections.get(session_id) or ExecutionConnections(
            directory / "session-data.sqlite3", create=False
        )
        try:
            connection = connections.borrow()
            try:
                # Live prefixes have no per-fragment power-loss guarantee. Request
                # lifecycle commits still use the database's durable transaction.
                connection.execute("PRAGMA synchronous = NORMAL")
                with connection:
                    return append_draft_chunk(connection, session_id, context_id, chunk)
            finally:
                connections.return_connection(connection)
        except sqlite3.Error as exc:
            raise PersistenceError(f"could not append response Draft: {exc}") from exc

    def load_response_draft(
        self, session_id: str, context_id: str, *, after_cursor: int = 0, limit: int | None = None
    ) -> ResponseDraftPage | None:
        if after_cursor < 0 or (limit is not None and limit < 1):
            raise ValueError("Draft cursor and page size must be positive")
        with self.session_read(session_id) as reader:
            self._require_session_dir(session_id)
            page = read_draft_page(
                reader.connection(), session_id, context_id, after_cursor=after_cursor, limit=limit
            )
            if page is not None:
                return page
            # A prepared request has no append target until it starts, but its
            # observation is already an empty page. Closed/replaced requests are absent.
            header = read_latest_header(reader.connection(), session_id)
            if (
                header is None
                or header.context_id != context_id
                or header.response_closed_at is not None
            ):
                return None
            return ResponseDraftPage(context_id=context_id)

    def record_request_retry(
        self, session_id: str, context_id: str, retry: ProviderRetryNotice
    ) -> None:
        with self.session_mutation(session_id):
            with connect_data(self._require_session_dir(session_id), mode="rw") as connection:
                header = read_request_header(connection, session_id, context_id)
                if header is None:
                    raise PersistenceError(f"request not found: {context_id}")
                write_header(connection, header.model_copy(update={"retry": retry}))
                advance_revision(connection, session_id)

    def settle_request_header(
        self,
        session_id: str,
        context_id: str,
        *,
        settled_at: datetime,
        provider_done: bool,
        usage_entry_id: str | None,
    ) -> ModelRequestHeader:
        with self.session_mutation(session_id):
            directory = self._require_session_dir(session_id)
            with connect_data(directory) as connection:
                header = read_request_header(connection, session_id, context_id)
            if header is None:
                raise PersistenceError(f"latest context does not match {context_id}")
            completed = header.settled(
                context_id=context_id,
                settled_at=settled_at,
                provider_done=provider_done,
                usage_entry_id=usage_entry_id,
            )
            if completed != header:
                with connect_data(directory, mode="rw") as connection:
                    write_header(connection, completed)
                    advance_revision(connection, session_id)
            return completed

    def load_request_header(self, session_id: str, context_id: str) -> ModelRequestHeader | None:
        with self.session_read(session_id) as reader:
            self._require_session_dir(session_id)
            return read_request_header(reader.connection(), session_id, context_id)

    def load_latest_context_header(self, session_id: str) -> ModelRequestHeader | None:
        with self.session_read(session_id) as reader:
            self._require_session_dir(session_id)
            return read_latest_header(reader.connection(), session_id)

    def load_latest_context(self, session_id: str) -> ContextSnapshotRecord | None:
        with self.session_read(session_id) as reader:
            try:
                request = read_request(reader.connection(), self._session_dir(session_id))
                if request is None:
                    return None
                header, body = request
                context = ContextSnapshotRecord.assemble(
                    header,
                    body,
                    {
                        identity: self.load_message(session_id, identity)
                        for identity in body.manifest.referenced_message_ids
                    },
                )
                return materialize_context_media(
                    context,
                    lambda identity: self._messages.media(
                        reader.connection(), self._require_session_dir(session_id), identity
                    ),
                )
            except (OSError, ValidationError, ValueError) as exc:
                raise PersistenceError(
                    f"could not load latest context for session {session_id}: {exc}"
                ) from exc

    def load_latest_request(
        self, session_id: str
    ) -> tuple[ModelRequestHeader, RequestManifestRecord] | None:
        with self.session_read(session_id) as reader:
            return read_request(reader.connection(), self._session_dir(session_id))

    def load_session(self, session_id: str) -> SessionSnapshot:
        with self.session_read(session_id) as reader:
            history = self._load_session_history(session_id)
            session_dir = self._session_dir(session_id)
            latest_request = read_request(reader.connection(), session_dir)
            # Records are decoded at their storage boundary; assembling a typed
            # container must not validate the entire history a second time.
            snapshot = SessionSnapshot.model_construct(
                session=history.session,
                request_headers=read_headers(reader.connection(), session_id),
                latest_manifest=latest_request[1] if latest_request is not None else None,
                messages=history.messages,
                runs=history.runs,
                compactions=history.compactions,
                tool_facts=load_tool_facts(reader.connection(), session_id),
                call_states=load_states(reader.connection(), session_id),
                inputs=sorted(
                    (
                        decode_versioned_record_json(
                            InputRecord, read_text_atomic(path), label="input"
                        )
                        for path in (session_dir / "inputs" / "records").glob("*.json")
                    ),
                    key=lambda record: record.item.queue_id,
                ),
                input_states=[
                    decode_versioned_record_json(InputState, row[0], label="input state")
                    for row in reader.connection().execute(
                        "SELECT payload FROM input_states WHERE session_id = ? ORDER BY input_id",
                        (session_id,),
                    )
                ],
                extension_states=self.load_session_extension_states(session_id),
                harness_versions=[
                    self._commits.load_harness(session_id, path.stem)
                    for path in sorted((session_dir / "harness").glob("*.json"))
                ],
            )
            validate_request_snapshot(
                session_id, snapshot.request_headers, snapshot.latest_manifest
            )
            validate_call_states(snapshot.call_states, snapshot.tool_facts, session_id=session_id)
        return snapshot

    def load_runtime_session(self, session_id: str) -> RuntimeSessionSnapshot:
        with self.session_read(session_id) as reader:
            session = self.load_session_record(session_id)
            connection = reader.connection()
            selected = {
                run.run_id: run
                for run in read_pending_runs(connection, session_id, session.history_epoch)
            }
            for identity in (session.active_run_id, session.last_run_id):
                if identity is not None and identity not in selected:
                    try:
                        selected[identity] = read_run(connection, session_id, identity)
                    except FileNotFoundError:
                        # Explicit recovery can clear an invalid Session pointer.
                        pass
            return RuntimeSessionSnapshot.model_construct(
                session=session, runs=sorted(selected.values(), key=_run_sort_key)
            )

    def load_pending_runs(self, session_id: str) -> list[RunRecord]:
        with self.session_read(session_id) as reader:
            session = self.load_session_record(session_id)
            return read_pending_runs(reader.connection(), session_id, session.history_epoch)

    def load_session_observation(self, session_id: str) -> SessionHistorySnapshot:
        with self.session_read(session_id):
            return self._load_session_history(session_id)

    def load_run_request_counts(self, session_id: str) -> tuple[RunRequestCounts, ...]:
        with self.session_read(session_id) as reader:
            return read_request_counts(reader.connection(), session_id)

    def load_runs(self, session_id: str) -> list[RunRecord]:
        with self.session_read(session_id):
            return self._load_runs(session_id)

    def _load_runs(self, session_id: str) -> list[RunRecord]:
        with self.session_read(session_id) as reader:
            return read_runs(reader.connection(), session_id)

    def _load_session_history(self, session_id: str) -> SessionHistorySnapshot:
        session_dir = self._session_dir(session_id)
        try:
            session = self.load_session_record(session_id)
            with self.session_read(session_id) as scope:
                messages = self._messages.materialize_many(
                    session_dir,
                    (
                        self._messages.entry(line, session_id=session_id)
                        for line in self._iter_jsonl(session_dir / "messages.jsonl")
                    ),
                    connection=scope.connection(),
                )
                compactions = read_compactions(scope.connection(), session_dir)
        except FileNotFoundError:
            raise
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(f"could not load session {session_id}: {exc}") from exc
        runs = self._load_runs(session_id)
        snapshot = SessionHistorySnapshot.model_construct(
            session=session,
            messages=messages,
            runs=runs,
            compactions=compactions,
        )
        if (
            session.context_selection.compaction_id is not None
            and snapshot.active_compaction is None
        ):
            raise PersistenceError(
                f"session {session_id} references missing compaction "
                f"{session.context_selection.compaction_id}"
            )
        return snapshot

    def load_active_compaction(self, session_id: str) -> CompactionRecord | None:
        with self.session_read(session_id):
            identity = self.load_session_record(session_id).context_selection.compaction_id
            return self.load_compaction(session_id, identity) if identity is not None else None

    def load_compaction(self, session_id: str, compaction_id: str) -> CompactionRecord:
        with self.session_read(session_id) as reader:
            try:
                return read_compaction(
                    reader.connection(), self._session_dir(session_id), compaction_id
                )
            except FileNotFoundError as exc:
                raise PersistenceError(
                    f"session {session_id} references missing compaction {compaction_id}"
                ) from exc

    def load_compactions(self, session_id: str) -> list[CompactionRecord]:
        with self.session_read(session_id) as reader:
            return read_compactions(reader.connection(), self._session_dir(session_id))

    def load_compactions_in_range(
        self, session_id: str, *, first_sequence: int, last_sequence: int
    ) -> list[CompactionRecord]:
        with self.session_read(session_id) as reader:
            rows = reader.connection().execute(
                "SELECT c.payload FROM locations AS m CROSS JOIN compactions AS c "
                "ON c.session_id = ? AND c.history_boundary_message_id = m.message_id "
                "WHERE m.position BETWEEN ? AND ? ORDER BY c.row_id",
                (session_id, first_sequence, last_sequence),
            )
            return [
                decode_versioned_record_json(CompactionRecord, row[0], label="compaction")
                for row in rows
            ]

    def load_observation_changes(
        self,
        session_id: str,
        *,
        after_revision: int,
        through_revision: int,
        limit: int,
        cursor: FactCursor | None = None,
    ) -> SessionFactPage:
        with self.session_read(session_id) as reader:
            return read_changes(
                reader.connection(),
                session_id,
                after_revision=after_revision,
                through_revision=through_revision,
                limit=limit,
                cursor=cursor,
            )

    def observation_revision(self, session_id: str) -> int:
        with self.session_read(session_id) as reader:
            return read_revision(reader.connection(), session_id)

    def load_session_record(self, session_id: str) -> SessionRecord:
        with self.session_read(session_id) as reader:
            return read_session(reader.connection(), session_id)

    def load_session_harness(self, session_id: str) -> tuple[SessionRecord, HarnessVersion]:
        with self.session_read(session_id) as reader:
            session = read_session(reader.connection(), session_id)
            harness = self._commits.load_harness(session_id, session.harness_version)
            return session, harness

    def load_messages_after(
        self,
        session_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[list[MessageRecord], bool]:
        with self.session_read(session_id) as reader:
            row = (
                reader.connection()
                .execute(
                    "SELECT offset FROM locations WHERE position > ? ORDER BY position LIMIT 1",
                    (after_sequence,),
                )
                .fetchone()
            )
            if row is None:
                return [], False
            messages, cursor = self._messages.page(
                reader.connection(),
                self._session_dir(session_id),
                session_id=session_id,
                cursor=row[0],
                limit=limit,
            )
            return messages, cursor is not None

    def load_messages_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> tuple[list[MessageRecord], int | None]:
        cursor, limit = _validated_page_args(cursor, limit)
        with self.session_read(session_id) as reader:
            return self._messages.page(
                reader.connection(),
                self._require_session_dir(session_id),
                session_id=session_id,
                cursor=cursor,
                limit=limit,
            )

    def load_message(self, session_id: str, message_id: str) -> MessageRecord:
        with self.session_read(session_id) as reader:
            directory = self._require_session_dir(session_id)
            message = self._messages.message(reader.connection(), directory, message_id)
            if message is not None:
                if message.session_id != session_id:
                    raise PersistenceError("message record belongs to another Session")
                return message
        raise PersistenceError(f"message not found in session {session_id}: {message_id}")

    def load_message_fact(self, session_id: str, message_id: str) -> MessageFact:
        with self.session_read(session_id) as reader:
            fact = self._messages.fact(
                reader.connection(), self._require_session_dir(session_id), message_id
            )
            if fact is None or fact.session_id != session_id:
                raise PersistenceError(f"message not found in session {session_id}: {message_id}")
            return fact

    def load_run_message_counts(self, session_id: str, run_id: str) -> RunMessageCounts:
        with self.session_read(session_id) as reader:
            return self._messages.index.read_counts(reader.connection(), run_id)

    def load_run_result_references(
        self, session_id: str, run_id: str, *, delegation_id: str
    ) -> RunResultReferences:
        with self.session_read(session_id) as reader:
            return self._messages.result_references(
                reader.connection(), run_id, delegation_id=delegation_id
            )

    def load_run_messages(self, session_id: str, run_id: str) -> list[MessageRecord]:
        with self.session_read(session_id) as reader:
            directory = self._require_session_dir(session_id)
            return self._messages.run(reader.connection(), directory, run_id, session_id=session_id)

    def run_message_end(self, session_id: str, run_id: str) -> int:
        with self.session_read(session_id) as reader:
            row = (
                reader.connection()
                .execute(
                    "SELECT position FROM locations WHERE run_id = ? "
                    "ORDER BY position DESC LIMIT 1",
                    (run_id,),
                )
                .fetchone()
            )
            return row[0] if row is not None else 0

    def has_run_messages(self, session_id: str, run_id: str) -> bool:
        with self.session_read(session_id) as reader:
            found = self._messages.index.last_reference(reader.connection(), "runs", run_id)
            if found is None:
                return False
            if found.run_id != run_id:
                raise PersistenceError("Run message lookup has a foreign identity")
            return True

    def has_input_messages(self, session_id: str) -> bool:
        with self.session_read(session_id) as reader:
            found = self._messages.index.last_reference(
                reader.connection(), "input_messages", "all"
            )
            if found is None:
                return False
            if found.role not in {"user", "system"}:
                raise PersistenceError("input history lookup has a non-input message")
            return True

    def find_last_assistant_message(self, session_id: str, run_id: str) -> MessageRecord | None:
        with self.session_read(session_id) as reader:
            directory = self._require_session_dir(session_id)
            found = self._messages.index.last_reference(
                reader.connection(), "assistant_runs", run_id
            )
            if found is None:
                return None
            if found.run_id != run_id or found.role != "assistant":
                raise PersistenceError("assistant lookup has a foreign message")
            return self._messages.resolve(
                reader.connection(), directory, found, session_id=session_id
            )

    def find_assistant_message(
        self, session_id: str, run_id: str, turn_id: str
    ) -> MessageRecord | None:
        with self.session_read(session_id) as reader:
            return self._messages.run_message(
                reader.connection(),
                self._require_session_dir(session_id),
                session_id=session_id,
                run_id=run_id,
                kind="assistant_turns",
                identity=turn_id,
            )

    def find_context_provider_message(
        self, session_id: str, run_id: str, delivery_key: str
    ) -> MessageRecord | None:
        with self.session_read(session_id) as reader:
            return self._messages.run_message(
                reader.connection(),
                self._require_session_dir(session_id),
                session_id=session_id,
                run_id=run_id,
                kind="context_deliveries",
                identity=delivery_key,
            )

    def find_queued_input_message(self, session_id: str, queue_id: str) -> MessageRecord | None:
        with self.session_read(session_id) as reader:
            directory = self._require_session_dir(session_id)
            return self._messages.queued(
                reader.connection(), directory, queue_id, session_id=session_id
            )

    def load_run(self, session_id: str, run_id: str) -> RunRecord:
        with self.session_read(session_id) as reader:
            return read_run(reader.connection(), session_id, run_id)

    def list_sessions(self) -> list[SessionRecord]:
        sessions = []
        for path in sorted(self.sessions_dir.glob("[!.]*/session-data.sqlite3")):
            try:
                sessions.append(self.load_session_record(path.parent.name))
            except FileNotFoundError:
                # A reserved resource directory can have unpublished snapshot data.
                continue
        return sessions

    def load_resource_references(self, session_id: str) -> list[SessionResourceReference]:
        with self.session_read(session_id) as reader:
            return sqlite_resources.read_references(reader.connection(), session_id)

    def load_resource_reference(
        self, session_id: str, resource_id: str
    ) -> SessionResourceReference:
        with self.session_read(session_id) as reader:
            return sqlite_resources.read_reference(reader.connection(), session_id, resource_id)

    def attach_resource_reference(
        self, session_id: str, reference: SessionResourceReference
    ) -> None:
        with self.session_mutation(session_id):
            self._require_session_dir(session_id)
            with self._commits.connect(session_id, mode="rw") as connection:
                sqlite_resources.attach_reference(connection, session_id, reference)

    def remove_resource_reference(self, session_id: str, resource_id: str) -> None:
        with self.session_mutation(session_id):
            self._require_session_dir(session_id)
            with self._commits.connect(session_id, mode="rw") as connection:
                sqlite_resources.remove_reference(connection, session_id, resource_id)

    def load_session_extension_states(self, session_id: str) -> list[SessionExtensionStateRecord]:
        return list(self.load_harness(session_id).state.states)

    def scan_catalog(self) -> SessionIndex:
        return self._catalog.scan()

    def rebuild_catalog(self) -> SessionIndex:
        return self._catalog.rebuild()

    def refresh_catalog(
        self,
        session_id: str,
    ) -> str | None:
        return self._catalog.try_update(session_id)

    def read_catalog(self) -> SessionIndex:
        return self._catalog.load()

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionRunLock | None:
        connections = ExecutionConnections(
            self._session_dir(session_id) / "session-data.sqlite3", create=False
        )

        def released() -> None:
            self._connections.pop(session_id)

        lock = NativeSessionRunLock(
            self._lock_path(session_id),
            session_id=session_id,
            run_id=run_id,
            connections=connections,
            on_release=released,
        )
        try:
            if not lock.acquire():
                return None
            self._connections[session_id] = connections
            return lock
        except OSError as exc:
            raise PersistenceError(
                f"could not acquire session lock for {session_id}: {exc}"
            ) from exc

    @contextmanager
    def session_read(self, session_id: str) -> Iterator[_SessionRead]:
        # Nested consumers share the whole read boundary, including its lock and
        # directory identity, rather than constructing another boundary per read.
        current = getattr(self._read_state, "scopes", {}).get(session_id)
        if current is not None:
            yield current
        else:
            with _SessionRead(self, session_id) as reader:
                yield reader

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

    def _require_session_dir(self, session_id: str) -> Path:
        session_dir = self._session_dir(session_id)
        session_path = session_dir / "session-data.sqlite3"
        if not session_path.is_file():
            raise FileNotFoundError(session_path)
        scope = getattr(self._read_state, "scopes", {}).get(session_id)
        if scope is not None:
            scope.connection()
        else:
            with self._commits.connect(session_id) as connection:
                require_session(connection, session_id)
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

    def _write_model_atomic(self, path: Path, model: OuroModel) -> None:
        write_model_atomic(path, model)

    def _write_json_atomic(self, path: Path, value: object) -> None:
        write_json_atomic(path, value)

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


class _SessionRead(SessionMutationLock):
    def __init__(self, store: LocalSessionStore, session_id: str) -> None:
        super().__init__(store._mutation_lock_path(session_id), session_id=session_id)
        self._store = store
        self._connection: sqlite3.Connection | None = None
        self._resources = ExitStack()

    def __enter__(self) -> Self:
        super().__enter__()
        try:
            if (self._store._session_dir(self.session_id) / "commit.json").exists():
                raise SessionRecoveryRequiredError(
                    f"Session {self.session_id} has a pending native decision; "
                    "enter session_mutation to recover it before reading"
                )
            self._store._observe_session_directory(self.session_id)
            scopes = getattr(self._store._read_state, "scopes", None)
            if scopes is None:
                scopes = self._store._read_state.scopes = {}
            scopes[self.session_id] = self
        except BaseException:
            super().release()
            raise
        return self

    def connection(self) -> sqlite3.Connection:
        assert self._acquired
        if self._connection is None:
            directory = self._store._session_dir(self.session_id)
            if not (directory / "session-data.sqlite3").is_file():
                raise FileNotFoundError(self.session_id)
            self._connection = self._resources.enter_context(
                connect_data(
                    directory,
                    connections=self._store._connections.get(self.session_id),
                )
            )
            require_session(self._connection, self.session_id)
        return self._connection

    def release(self) -> None:
        try:
            if self._acquired:
                self._store._read_state.scopes.pop(self.session_id)
                self._resources.close()
        finally:
            super().release()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._resources.__exit__(exc_type, exc, traceback)
        finally:
            self.release()


class _SessionMutation(SessionMutationLock):
    def __init__(self, store: LocalSessionStore, session_id: str) -> None:
        super().__init__(store._mutation_lock_path(session_id), session_id=session_id)
        self._store = store

    def __enter__(self) -> Self:
        super().__enter__()
        try:
            self._store._observe_session_directory(self.session_id)
            self._store._commits.finish(self.session_id)
        except OSError as exc:
            self.release()
            raise PersistenceError(f"could not recover Session {self.session_id}: {exc}") from exc
        except BaseException:
            self.release()
            raise
        return self
