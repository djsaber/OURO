from __future__ import annotations

from collections.abc import Generator, Iterable, Mapping
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol

from pydantic import Field

from ouro.core.agents import DelegationRecord
from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionRecord
from ouro.core.context_snapshots import ContextSnapshotHeader, ContextSnapshotRecord
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessVersion
from ouro.core.message_frames import MessageTruncation
from ouro.core.messages import MessageRecord
from ouro.core.resources import ResourceRecord, SessionResourceReference
from ouro.core.session_commits import SessionCommit, SessionCommitResult
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.index import SessionIndex

if TYPE_CHECKING:
    from ouro.sessions.portable import SessionBundle


class RuntimeSessionSnapshot(OuroModel):
    """Canonical state required to execute a Session, excluding replay events."""

    session: SessionRecord
    messages: list[MessageRecord] = Field(default_factory=list)
    runs: list[RunRecord] = Field(default_factory=list)
    compactions: list[CompactionRecord] = Field(default_factory=list)

    @property
    def active_compaction(self) -> CompactionRecord | None:
        active_id = self.session.active_compaction_id
        if active_id is None:
            return None
        for compaction in self.compactions:
            if compaction.compaction_id == active_id:
                return compaction
        return None


class SessionSnapshot(RuntimeSessionSnapshot):
    """Complete Session view for recovery, inspection, export, and replay."""

    events: list[EventRecord] = Field(default_factory=list)
    extension_states: list[SessionExtensionStateRecord] = Field(default_factory=list)
    harness_versions: list[HarnessVersion] = Field(default_factory=list)
    commits: list[SessionCommitResult] = Field(default_factory=list)


class SessionObservationState(RuntimeSessionSnapshot):
    """Atomic persistence view for Host snapshot/live event handoff."""

    active_run_events: list[EventRecord] = Field(default_factory=list)
    event_high_water: int = Field(default=0, ge=0)


class SessionLock(Protocol):
    def release(self) -> None: ...


class SessionRepository(Protocol):
    """Canonical Session persistence required by application behavior.

    Deployment paths, initialization and disposable catalog maintenance are
    deliberately excluded. Implementations must provide real mutation and
    ordered-event guarantees; silently degrading either guarantee is unsafe.
    """

    def load_harness(self, session_id: str, version_id: str | None = None) -> HarnessVersion: ...

    def commit_session(self, request: SessionCommit) -> SessionCommitResult: ...

    def load_commit(self, session_id: str, operation_id: str) -> SessionCommitResult | None: ...

    def create_session(self, session: SessionRecord) -> None: ...

    def create_delegation(self, delegation: DelegationRecord) -> DelegationRecord: ...

    def load_delegation(self, root_session_id: str, delegation_id: str) -> DelegationRecord: ...

    def load_delegation_by_id(self, delegation_id: str) -> DelegationRecord: ...

    def save_delegation(
        self, delegation: DelegationRecord, *, expected_revision: int
    ) -> DelegationRecord: ...

    def list_delegations(
        self, root_session_id: str, *, parent_session_id: str | None = None
    ) -> list[DelegationRecord]: ...

    def find_delegation_by_child_session(
        self, root_session_id: str, child_session_id: str
    ) -> DelegationRecord | None: ...

    def find_delegation_by_idempotency_key(
        self, root_session_id: str, idempotency_key: str
    ) -> DelegationRecord | None: ...

    def create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None = None,
        harness_versions: list[HarnessVersion] | None = None,
        compactions: list[CompactionRecord] | None = None,
        commits: list[SessionCommitResult] | None = None,
        resource_contents: Iterable[tuple[ResourceRecord, bytes]] = (),
        *,
        prepared_resources: bool = False,
    ) -> None: ...

    def save_session(self, session: SessionRecord) -> None: ...

    def trash_session_tree(
        self,
        root_session_id: str,
        session_ids: tuple[str, ...],
    ) -> str: ...

    def append_message(self, message: MessageRecord) -> None: ...

    def append_messages(self, messages: list[MessageRecord]) -> None: ...

    def load_messages_tail(self, session_id: str, *, limit: int = 100) -> list[MessageRecord]:
        """Read at most limit canonical messages, newest first."""
        ...

    def iter_messages_before(
        self, session_id: str, *, before: int | None = None
    ) -> Generator[tuple[int, MessageRecord]]:
        """Yield newest-first messages with exclusive, backend-local cursors.

        A cursor is valid only for its Session and history epoch. Close the
        iterator after reading the required boundary to release its reader.
        """
        ...

    def truncate_messages(
        self,
        session_id: str,
        *,
        before_message_id: str,
        expected_last_message_id: str,
    ) -> MessageTruncation:
        """Atomically remove a message and its suffix, restoring its before-state.

        Reject unknown/unrecorded boundaries and stale history heads before
        writing. Run records and events remain audit facts. Execution ownership
        and application-specific restrictions belong to the caller.
        """
        ...

    def append_event(self, event: EventRecord) -> None: ...

    def append_events(self, events: list[EventRecord]) -> None:
        """Append one Session's existing events within a staging store or transaction."""
        ...

    def append_ordered_event(self, event: EventRecord) -> EventRecord: ...

    def save_run(self, run: RunRecord) -> None: ...

    def save_latest_context(self, context: ContextSnapshotRecord) -> None: ...

    def load_latest_context(self, session_id: str) -> ContextSnapshotRecord | None: ...

    def load_latest_context_header(self, session_id: str) -> ContextSnapshotHeader | None:
        """Read request status without materializing provider arguments."""
        ...

    def append_compaction(self, compaction: CompactionRecord) -> None: ...

    def activate_compaction(self, session_id: str, compaction_id: str) -> SessionRecord: ...

    def load_session(self, session_id: str) -> SessionSnapshot: ...

    def load_runtime_session(
        self,
        session_id: str,
    ) -> RuntimeSessionSnapshot: ...

    def load_session_record(self, session_id: str) -> SessionRecord: ...

    def load_session_harness(self, session_id: str) -> tuple[SessionRecord, HarnessVersion]:
        """Read a Session and its referenced Harness at one consistency boundary.

        The mutable Session controls projection is isolated from the checkpoint.
        """
        ...

    def load_session_observation(self, session_id: str) -> SessionObservationState: ...

    def load_active_compaction(self, session_id: str) -> CompactionRecord | None: ...

    def load_compactions(self, session_id: str) -> list[CompactionRecord]: ...

    def latest_event_sequence(self, session_id: str) -> int: ...

    def load_messages_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> tuple[list[MessageRecord], int | None]: ...

    def load_message(self, session_id: str, message_id: str) -> MessageRecord: ...

    def load_run_messages(self, session_id: str, run_id: str) -> list[MessageRecord]:
        """Read only one Run's canonical messages, in journal order."""
        ...

    def find_queued_input_message(self, session_id: str, queue_id: str) -> MessageRecord | None:
        """Find the canonical receipt of an already consumed queued input."""
        ...

    def load_events_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        run_id: str | None = None,
    ) -> tuple[list[EventRecord], int | None]: ...

    def load_events_after(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[list[EventRecord], bool]: ...

    def load_run(self, session_id: str, run_id: str) -> RunRecord: ...

    def load_runs(self, session_id: str) -> list[RunRecord]: ...

    def list_sessions(self) -> list[SessionRecord]: ...

    def load_resource_references(self, session_id: str) -> list[SessionResourceReference]: ...

    def attach_resource_reference(
        self,
        session_id: str,
        reference: SessionResourceReference,
    ) -> None: ...

    def remove_resource_reference(self, session_id: str, resource_id: str) -> None: ...

    def load_session_extension_states(
        self,
        session_id: str,
    ) -> list[SessionExtensionStateRecord]: ...

    def session_mutation(
        self,
        session_id: str,
    ) -> AbstractContextManager[object, None]: ...

    def session_read(self, session_id: str) -> AbstractContextManager[object, bool | None]:
        """Compose synchronous reads of one Session at a consistent boundary.

        The boundary holds no execution ownership and must end before projection,
        rendering or asynchronous waiting. SQLite readers do not reserve a writer.
        """
        ...

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionLock | None: ...

    def is_session_locked(self, session_id: str) -> bool: ...


class SessionCatalogStore(Protocol):
    """Disposable projection whose reads never inspect canonical Session state."""

    def read_catalog(self) -> SessionIndex: ...

    def refresh_catalog(
        self,
        session_id: str,
    ) -> str | None: ...


class SessionStore(SessionRepository, SessionCatalogStore, Protocol):
    """Complete in-process store composed from canonical and catalog ports."""

    def import_bundle(
        self,
        bundle: SessionBundle,
        *,
        replace_session_ids: tuple[str, ...] = (),
        resource_contents: Mapping[str, Mapping[str, bytes]] | None = None,
    ) -> None:
        """Publish a bundle, replacing explicitly selected trees with failure rollback.

        The application owns execution locks and resolves complete target trees.
        Other identity collisions are rejected at publication time.
        """
        ...
