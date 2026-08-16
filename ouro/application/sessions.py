from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Protocol

from ouro.application.dto import Diagnostic
from ouro.application.runs import abort_run_state, find_run
from ouro.application.session_history import (
    apply_extension_branch_policies,
    copy_branch_compactions,
    copy_branch_messages,
    project_replay_frames,
    select_branch_messages,
)
from ouro.application.session_models import (
    SessionBranchResult,
    SessionCatalogEntry,
    SessionCatalogQuery,
    SessionCatalogResult,
    SessionContextSnapshot,
    SessionContextStatus,
    SessionDetail,
    SessionEventPage,
    SessionExport,
    SessionMessagePage,
    SessionMutationResult,
    SessionReplay,
    SessionTrashResult,
    SessionView,
)
from ouro.application.session_recovery import SessionRecoveryService
from ouro.core.agents import DelegationRecord
from ouro.core.approvals import SESSION_APPROVAL_MODES, SessionApprovalMode
from ouro.core.base import new_id, utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextMetrics, ContextView
from ouro.core.context_snapshots import context_view_digest
from ouro.core.errors import (
    PersistenceError,
    SessionBranchError,
    SessionBusyError,
    SessionNotFoundError,
)
from ouro.core.events import EventRecord
from ouro.core.interactions import SESSION_INTERACTION_MODES, SessionInteractionMode
from ouro.core.messages import MessageRecord, ModelMessage
from ouro.core.reasoning import ReasoningRequest
from ouro.core.results import RunResult
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord, SessionTitleSource
from ouro.sessions import SessionIndexEntry, SessionLock, SessionSnapshot, SessionStore

if TYPE_CHECKING:
    from ouro.application.resources import ResourceService

_MAX_TITLE_LENGTH = 160


class SessionTitleGenerator(Protocol):
    async def __call__(
        self,
        session: SessionView,
        messages: tuple[MessageRecord, ...],
    ) -> str: ...


class SessionService:
    """Application boundary for canonical session commands and catalog queries."""

    def __init__(
        self,
        store: SessionStore,
        *,
        resources: ResourceService | None = None,
    ) -> None:
        self._store = store
        self._resources = resources
        self._recovery = SessionRecoveryService(store)

    def create(
        self,
        *,
        session_id: str | None = None,
        title: str = "New session",
        title_source: SessionTitleSource = "fallback",
        workspace: str | None = None,
        current_model: str | None = None,
        reasoning_request: ReasoningRequest | None = None,
        approval_mode: SessionApprovalMode = "inherit",
        interaction_mode: SessionInteractionMode = "interactive",
    ) -> SessionMutationResult:
        normalized_title = _normalize_title(title)
        session = SessionRecord(
            session_id=session_id or new_id("ses"),
            title=normalized_title,
            title_source=title_source,
            title_locked=title_source == "user",
            workspace=workspace,
            current_model=current_model,
            reasoning_request=reasoning_request or ReasoningRequest(),
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
        )
        self._store.create_session(session)
        return SessionMutationResult(
            session=SessionView.from_record(session),
            diagnostics=self._refresh_index_diagnostics(session.session_id),
        )

    def open(self, session_id: str, *, recover: bool = True) -> SessionDetail:
        try:
            session = self._store.load_session_record(session_id)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc
        if recover and (
            session.active_run_id is not None
            or session.status in {"running", "waiting_approval", "waiting_user"}
        ):
            session = self._recovery.recover_runtime(session_id).session
        recent_run_id = session.active_run_id or session.last_run_id
        recent_run = (
            self._store.load_run(session_id, recent_run_id) if recent_run_id is not None else None
        )
        diagnostics = self._refresh_index_diagnostics(session_id) if recover else ()
        return SessionDetail(
            session=SessionView.from_record(session),
            recent_run=recent_run,
            active_compaction=self._store.load_active_compaction(session_id),
            diagnostics=diagnostics,
        )

    def run_result(self, session_id: str, run_id: str) -> RunResult:
        """Read one durable Run without exposing SessionStore to Hosts."""

        snapshot = self._store.load_runtime_session(session_id)
        try:
            run = find_run(snapshot.runs, run_id)
        except KeyError as exc:
            raise PersistenceError(f"Run not found: {run_id}") from exc
        final_text = next(
            (
                message.text_content
                for message in reversed(snapshot.messages)
                if message.run_id == run_id and message.role == "assistant"
            ),
            "",
        )
        return RunResult(session=snapshot.session, run=run, final_text=final_text)

    def run_history(self, session_id: str) -> tuple[RunRecord, ...]:
        """Read lightweight durable Run facts without replaying the event journal."""

        self._require_session(session_id)
        return tuple(self._store.load_runs(session_id))

    def context_status(self, session_id: str) -> SessionContextStatus | None:
        self._require_session(session_id)
        record = self._store.load_latest_context(session_id)
        return SessionContextStatus.from_record(record) if record is not None else None

    def effective_context_metrics(self, session_id: str) -> ContextMetrics | None:
        """Return the newest effective context estimate, including manual compaction.

        A context snapshot records an actual provider request. Manual compaction
        does not issue one, so its durable post-compaction projection is a
        separate fact and supersedes an older request estimate until the next
        model request is prepared.
        """

        self._require_session(session_id)
        latest = self._store.load_latest_context(session_id)
        active = self._store.load_active_compaction(session_id)
        if active is not None and active.context_metrics_after is not None:
            if latest is None or active.created_at > latest.prepared_at:
                return active.context_metrics_after
        return latest.metrics if latest is not None else None

    def compactions(self, session_id: str) -> tuple[CompactionRecord, ...]:
        """Read durable compaction boundaries without loading message or event history."""

        self._require_session(session_id)
        return tuple(self._store.load_compactions(session_id))

    def context(self, session_id: str) -> SessionContextSnapshot | None:
        self._require_session(session_id)
        record = self._store.load_latest_context(session_id)
        if record is None:
            return None
        if any(message.resource_refs for message in record.source_messages):
            if self._resources is None:
                raise PersistenceError(
                    "session context contains resources but no ResourceService is available"
                )
            source_messages = self._resources.project_model_messages(
                record.source_messages,
                supports_vision=record.supports_vision,
            )
        else:
            source_messages = record.source_messages
        context = ContextView(
            context_id=record.context_id,
            system_prompt=record.system_prompt,
            model_messages=[
                *record.injected_messages,
                *(ModelMessage.from_record(message) for message in source_messages),
            ],
            tool_definitions=record.tool_definitions,
            metrics=record.metrics,
            context_metadata=record.context_metadata,
        )
        digest = context_view_digest(context)
        if digest != record.context_digest:
            raise PersistenceError(
                f"context snapshot {record.context_id} does not match its stored digest"
            )
        return SessionContextSnapshot(
            status=SessionContextStatus.from_record(record),
            provider_context=context.provider_context(),
            context_digest=digest,
        )

    def export(self, session_id: str) -> SessionExport:
        snapshot = self._load_snapshot(session_id, recover=False)
        return SessionExport(
            session=SessionView.from_record(snapshot.session),
            messages=tuple(snapshot.messages),
            events=tuple(snapshot.events),
            runs=tuple(snapshot.runs),
            compactions=tuple(snapshot.compactions),
            extension_states=tuple(snapshot.extension_states),
        )

    def message_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> SessionMessagePage:
        messages, next_cursor = self._store.load_messages_page(
            session_id,
            cursor=cursor,
            limit=limit,
        )
        return SessionMessagePage(
            session_id=session_id,
            messages=tuple(messages),
            next_cursor=next_cursor,
        )

    def message(self, session_id: str, message_id: str) -> MessageRecord:
        """Read one canonical message by its stable event-facing identity."""

        return self._store.load_message(session_id, message_id)

    def event_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        run_id: str | None = None,
    ) -> SessionEventPage:
        events, next_cursor = self._store.load_events_page(
            session_id,
            cursor=cursor,
            limit=limit,
            run_id=run_id,
        )
        return SessionEventPage(
            session_id=session_id,
            events=tuple(events),
            next_cursor=next_cursor,
        )

    def catalog(self, query: SessionCatalogQuery | None = None) -> SessionCatalogResult:
        index = self._store.read_catalog()
        entries = _filter_entries(index.sessions, query or SessionCatalogQuery())
        return SessionCatalogResult(
            entries=tuple(SessionCatalogEntry.from_index(entry) for entry in entries),
        )

    def rename(self, session_id: str, title: str) -> SessionMutationResult:
        return self._update_session(
            session_id,
            title=_normalize_title(title),
            title_source="user",
            title_locked=True,
        )

    def set_archived(self, session_id: str, archived: bool) -> SessionMutationResult:
        if archived:
            session = self._load_session_record(session_id)
            if session.session_kind == "primary":
                active = self._active_or_unresolved_agent_descendants(
                    self._store.list_delegations(session.agent_root_session_id)
                )
                if active:
                    raise SessionBusyError(
                        self._agent_descendant_conflict(
                            session_id,
                            active,
                            action="archive",
                            qualifier="active or unresolved",
                        )
                    )
        return self._update_session(session_id, archived=archived)

    def set_pinned(self, session_id: str, pinned: bool) -> SessionMutationResult:
        return self._update_session(session_id, pinned=pinned)

    def set_tags(self, session_id: str, tags: Iterable[str]) -> SessionMutationResult:
        normalized = sorted({tag.strip() for tag in tags if tag.strip()})
        return self._update_session(session_id, tags=normalized)

    def set_approval_mode(
        self,
        session_id: str,
        approval_mode: SessionApprovalMode,
    ) -> SessionMutationResult:
        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        return self._update_session(session_id, approval_mode=approval_mode)

    def set_interaction_mode(
        self,
        session_id: str,
        interaction_mode: SessionInteractionMode,
    ) -> SessionMutationResult:
        if interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"invalid session interaction mode: {interaction_mode}")
        return self._update_session(session_id, interaction_mode=interaction_mode)

    def set_policies(
        self,
        session_id: str,
        *,
        approval_mode: SessionApprovalMode,
        interaction_mode: SessionInteractionMode,
    ) -> SessionMutationResult:
        """Persist the approval and interaction policy pair atomically."""

        if approval_mode not in SESSION_APPROVAL_MODES:
            raise ValueError(f"invalid session approval mode: {approval_mode}")
        if interaction_mode not in SESSION_INTERACTION_MODES:
            raise ValueError(f"invalid session interaction mode: {interaction_mode}")
        return self._update_session(
            session_id,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
        )

    def set_reasoning_request(
        self,
        session_id: str,
        reasoning_request: ReasoningRequest,
    ) -> SessionMutationResult:
        """Persist endpoint-neutral reasoning intent after application validation."""

        return self._update_session(session_id, reasoning_request=reasoning_request)

    def set_afk_mode(self, session_id: str, *, enabled: bool) -> SessionMutationResult:
        """Apply the AFK policy preset atomically to an idle Session."""

        return self.set_policies(
            session_id,
            approval_mode="auto" if enabled else "inherit",
            interaction_mode="unattended" if enabled else "interactive",
        )

    async def generate_title(
        self,
        session_id: str,
        generator: SessionTitleGenerator,
    ) -> SessionMutationResult:
        session = self._load_session_record(session_id)
        if session.title_locked or session.title_source == "user":
            return SessionMutationResult(session=SessionView.from_record(session))
        messages = self._load_messages(session_id)
        try:
            generated = await generator(
                SessionView.from_record(session),
                tuple(messages),
            )
            title = _normalize_title(generated)
        except Exception as exc:  # noqa: BLE001 - optional title provider isolation boundary
            return SessionMutationResult(
                session=SessionView.from_record(session),
                diagnostics=(
                    Diagnostic(
                        code="session_title_generation_failed",
                        severity="warning",
                        component="sessions.title",
                        message=f"title generator failed ({type(exc).__name__})",
                        session_id=session_id,
                        recoverable=True,
                        suggested_action="Retry title generation or rename the session manually.",
                    ),
                ),
            )

        try:
            latest = self._load_session_record(session_id)
            if latest.title_locked or latest.title_source == "user":
                return SessionMutationResult(session=SessionView.from_record(latest))
            return self._update_session(
                session_id,
                title=title,
                title_source="generated",
                title_locked=False,
            )
        except SessionBusyError as exc:
            latest = self._load_session_record(session_id)
            return SessionMutationResult(
                session=SessionView.from_record(latest),
                diagnostics=(
                    Diagnostic(
                        code="session_title_update_deferred",
                        severity="info",
                        component="sessions.title",
                        message=str(exc),
                        session_id=session_id,
                        recoverable=True,
                        suggested_action="Retry after the active run settles.",
                    ),
                ),
            )

    def trash_scope(self, session_id: str) -> tuple[str, tuple[str, ...]]:
        root_session_id, records = self._trash_scope_records(session_id)
        return root_session_id, tuple(record.session_id for record in records)

    async def stop_and_trash(
        self,
        session_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> SessionTrashResult:
        """Cooperatively stop live Runs in one Session tree, then trash it atomically."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        _, records = self._trash_scope_records(session_id)
        live_records = [
            record
            for record in reversed(records)
            if self._store.is_session_locked(record.session_id)
        ]
        for record in live_records:
            abort_run_state(
                self._store,
                session_id=record.session_id,
                run_id=record.active_run_id,
                reason="Session tree removal requested",
            )
        deadline = monotonic() + timeout_seconds
        while live_records:
            live_records = [
                record
                for record in live_records
                if self._store.is_session_locked(record.session_id)
            ]
            if not live_records:
                break
            if monotonic() >= deadline:
                identities = ", ".join(record.session_id for record in live_records)
                raise SessionBusyError(
                    f"timed out waiting for live Session Runs to stop before removal: {identities}"
                )
            await asyncio.sleep(0.05)
        return self.trash(session_id)

    def trash(self, session_id: str) -> SessionTrashResult:
        root_session_id, records = self._trash_scope_records(session_id)
        session_ids = tuple(record.session_id for record in records)
        active_session_ids = tuple(
            record.session_id
            for record in records
            if record.status in {"running", "waiting_approval", "waiting_user"}
            or record.active_run_id is not None
        )
        locks: list[SessionLock] = []
        lock_owner = new_id("trash_tree")
        try:
            for record in records:
                lock = self._store.acquire_session_lock(record.session_id, lock_owner)
                if lock is None:
                    raise SessionBusyError(
                        f"cannot remove Session tree while a live runtime owns {record.session_id}"
                    )
                locks.append(lock)

            resource_ids: list[str] = []
            diagnostics: list[Diagnostic] = []
            if self._resources is not None:
                for record in records:
                    try:
                        resource_ids.extend(self._resources.session_resource_ids(record.session_id))
                    except PersistenceError as exc:
                        diagnostics.append(
                            Diagnostic(
                                code="session_resource_index_incompatible",
                                severity="warning",
                                component="sessions.lifecycle",
                                message=(
                                    "Resource references could not be inspected before the "
                                    f"Session was moved to trash: {exc}"
                                ),
                                session_id=record.session_id,
                                recoverable=True,
                                suggested_action="Run resource garbage collection later.",
                            )
                        )
            trash_id = self._store.trash_session_tree(root_session_id, session_ids)
        finally:
            for lock in reversed(locks):
                lock.release()

        for removed_session_id in session_ids:
            diagnostics.extend(self._refresh_index_diagnostics(removed_session_id))
        if active_session_ids:
            diagnostics.append(
                Diagnostic(
                    code="unfinished_session_tree_trashed",
                    severity="warning",
                    component="sessions.lifecycle",
                    message=(
                        "Unfinished durable runs were discarded with the removed Session tree."
                    ),
                    session_id=root_session_id,
                    recoverable=False,
                    metadata={"session_ids": list(active_session_ids)},
                )
            )
        if self._resources is not None and resource_ids:
            try:
                self._resources.collect_after_session_trash(resource_ids)
            except Exception as exc:  # noqa: BLE001 - trash is canonical; GC is retryable.
                diagnostics.append(
                    Diagnostic(
                        code="resource_collection_deferred",
                        severity="warning",
                        component="resources.gc",
                        message=f"resource collection deferred ({type(exc).__name__})",
                        session_id=root_session_id,
                        recoverable=True,
                        suggested_action="Retry resource garbage collection.",
                    )
                )
        return SessionTrashResult(
            session_id=root_session_id,
            trash_id=trash_id,
            session_ids=session_ids,
            diagnostics=tuple(diagnostics),
        )

    def _trash_scope_records(
        self,
        session_id: str,
    ) -> tuple[str, tuple[SessionRecord, ...]]:
        try:
            requested = self._store.load_session_record(session_id)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc
        all_records = {record.session_id: record for record in self._store.list_sessions()}
        root = requested if requested.session_kind == "primary" else None
        ancestor = requested
        visited = {requested.session_id}
        while root is None and ancestor.parent_session_id is not None:
            parent = all_records.get(ancestor.parent_session_id)
            if parent is None or parent.session_id in visited:
                break
            visited.add(parent.session_id)
            if parent.session_kind == "primary":
                root = parent
                break
            ancestor = parent
        if root is None:
            candidate = all_records.get(requested.root_session_id or "")
            if candidate is not None and candidate.session_kind == "primary":
                root = candidate

        root_session_id = (
            root.session_id
            if root is not None
            else requested.root_session_id or requested.session_id
        )
        scoped_ids = {root.session_id if root is not None else requested.session_id}
        changed = True
        while changed:
            changed = False
            for record in all_records.values():
                if (
                    record.session_kind == "subagent"
                    and record.parent_session_id in scoped_ids
                    and record.session_id not in scoped_ids
                ):
                    scoped_ids.add(record.session_id)
                    changed = True
        if root is None:
            scoped_ids.update(
                record.session_id
                for record in all_records.values()
                if record.session_kind == "subagent" and record.root_session_id == root_session_id
            )
        scoped_ids.add(requested.session_id)
        records = [all_records[record_id] for record_id in scoped_ids]
        records.sort(
            key=lambda record: (
                record.session_id != root_session_id,
                record.session_id,
            )
        )
        return root_session_id, tuple(records)

    @staticmethod
    def _agent_descendant_conflict(
        session_id: str,
        relations: Iterable[DelegationRecord],
        *,
        action: str,
        qualifier: str,
    ) -> str:
        relation_list = list(relations)
        identities = ", ".join(relation.delegation_id for relation in relation_list[:4])
        suffix = "" if len(relation_list) <= 4 else ", ..."
        return (
            f"cannot {action} root Session {session_id}; "
            f"{len(relation_list)} {qualifier} Agent descendant(s) are in scope "
            f"({identities}{suffix})"
        )

    def _active_or_unresolved_agent_descendants(
        self,
        relations: Iterable[DelegationRecord],
    ) -> list[DelegationRecord]:
        active: list[DelegationRecord] = []
        for relation in relations:
            if relation.child_session_id is None or relation.current_child_run_id is None:
                if relation.spawn_state != "failed":
                    active.append(relation)
                continue
            try:
                run = self._store.load_run(
                    relation.child_session_id,
                    relation.current_child_run_id,
                )
            except FileNotFoundError:
                if relation.spawn_state != "failed":
                    active.append(relation)
                continue
            if run.status in ACTIVE_RUN_STATUSES:
                active.append(relation)
        return active

    def branch(
        self,
        source_session_id: str,
        message_id: str | None,
    ) -> SessionBranchResult:
        """Fork through any canonical message, or from an empty history boundary."""

        self._load_session_record(source_session_id)
        lock = self._store.acquire_session_lock(source_session_id, new_id("branch"))
        if lock is None:
            raise SessionBusyError(
                f"cannot branch Session while a live Run owns it: {source_session_id}"
            )
        try:
            with self._store.session_mutation(source_session_id):
                return self._branch_locked(
                    source_session_id,
                    message_id,
                )
        finally:
            lock.release()

    def _branch_locked(
        self,
        source_session_id: str,
        message_id: str | None,
    ) -> SessionBranchResult:
        source = self._load_session_record(source_session_id)
        messages = self._load_messages(source_session_id)
        runs = self._store.load_runs(source_session_id)
        compactions = self._store.load_compactions(source_session_id)
        extension_states = self._store.load_session_extension_states(source_session_id)
        if (
            source.active_run_id is not None
            or source.status in {"running", "waiting_approval", "waiting_user"}
            or any(run.status in ACTIVE_RUN_STATUSES for run in runs)
        ):
            raise SessionBusyError(
                f"cannot branch Session while it has an active Run: {source_session_id}"
            )
        selected_messages, source_message = select_branch_messages(messages, message_id)

        now = utc_now()
        branch = SessionRecord(
            root_session_id=source.root_session_id or source.session_id,
            parent_session_id=source.session_id,
            forked_from_message_id=(source_message.message_id if source_message else None),
            forked_from_run_id=(source_message.run_id if source_message else None),
            title=_branch_title(source.title),
            title_source="fallback",
            title_locked=False,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
            workspace=source.workspace,
            workspace_trusted=source.workspace_trusted,
            current_model=source.current_model,
            reasoning_request=source.reasoning_request,
            approval_mode=source.approval_mode,
            interaction_mode=source.interaction_mode,
            tags=list(source.tags),
        )
        selected_messages, extension_states = apply_extension_branch_policies(
            selected_messages,
            source_message.message_id if source_message else None,
            extension_states,
            branch.session_id,
        )
        resource_ids = list(
            dict.fromkeys(
                resource_id
                for message in selected_messages
                for resource_id in message.resource_refs
            )
        )
        if resource_ids and self._resources is None:
            raise SessionBranchError(
                "branch contains resource references; ResourceService must copy their records "
                "before this branch can be published"
            )
        copied_messages, message_id_map = copy_branch_messages(
            branch.session_id,
            selected_messages,
        )
        copied_compactions = copy_branch_compactions(
            branch.session_id,
            selected_messages,
            copied_messages,
            compactions,
            source.active_compaction_id,
            message_id_map,
        )
        branch = branch.model_copy(
            update={
                "last_message_id": copied_messages[-1].message_id if copied_messages else None,
                "message_count": len(copied_messages),
                "active_compaction_id": (
                    copied_compactions[-1].compaction_id if copied_compactions else None
                ),
            }
        )
        resource_references = (
            self._resources.branch_references(
                source_session_id,
                branch.session_id,
                resource_ids,
            )
            if self._resources is not None
            else []
        )
        self._store.create_session_snapshot(
            branch,
            copied_messages,
            resource_references=resource_references,
            extension_states=extension_states,
            compactions=copied_compactions,
        )
        return SessionBranchResult(
            session=SessionView.from_record(branch),
            source_session_id=source_session_id,
            source_message_id=(source_message.message_id if source_message else None),
            message_id_map=message_id_map,
            diagnostics=self._refresh_index_diagnostics(branch.session_id),
        )

    def replay(self, session_id: str, *, run_id: str | None = None) -> SessionReplay:
        events = self._load_events(session_id, run_id=run_id)
        return SessionReplay(
            session_id=session_id,
            run_id=run_id,
            frames=project_replay_frames(events),
        )

    def _update_session(self, session_id: str, **changes: object) -> SessionMutationResult:
        # Metadata mutations are independent of the append-only message and event
        # journals. Loading the complete aggregate here makes a constant-size
        # policy change scale with the lifetime of the conversation.
        self._load_session_record(session_id)
        lock = self._store.acquire_session_lock(session_id, new_id("session"))
        if lock is None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        try:
            with self._store.session_mutation(session_id):
                current = self._load_session_record(session_id)
                now = utc_now()
                updated = current.model_copy(
                    update={
                        **changes,
                        "updated_at": now,
                        "last_activity_at": now,
                    }
                )
                self._store.save_session(updated)
        finally:
            lock.release()
        return SessionMutationResult(
            session=SessionView.from_record(updated),
            diagnostics=self._refresh_index_diagnostics(session_id),
        )

    def _refresh_index_diagnostics(self, session_id: str) -> tuple[Diagnostic, ...]:
        error = self._store.refresh_catalog(session_id)
        if error is None:
            return ()
        return (
            Diagnostic(
                code="session_index_refresh_failed",
                severity="warning",
                component="sessions.catalog",
                message=error,
                session_id=session_id,
                recoverable=True,
                suggested_action="Rebuild the session index.",
            ),
        )

    def _load_snapshot(self, session_id: str, *, recover: bool) -> SessionSnapshot:
        try:
            if recover:
                return self._recovery.recover(session_id)
            return self._store.load_session(session_id)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc

    def _load_messages(self, session_id: str) -> list[MessageRecord]:
        messages: list[MessageRecord] = []
        cursor = 0
        try:
            while True:
                page, next_cursor = self._store.load_messages_page(
                    session_id,
                    cursor=cursor,
                    limit=1000,
                )
                messages.extend(page)
                if next_cursor is None:
                    return messages
                cursor = next_cursor
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc

    def _load_events(
        self,
        session_id: str,
        *,
        run_id: str | None,
    ) -> list[EventRecord]:
        events: list[EventRecord] = []
        cursor = 0
        try:
            while True:
                page, next_cursor = self._store.load_events_page(
                    session_id,
                    cursor=cursor,
                    limit=1000,
                    run_id=run_id,
                )
                events.extend(page)
                if next_cursor is None:
                    return events
                cursor = next_cursor
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc

    def _load_session_record(self, session_id: str) -> SessionRecord:
        try:
            return self._store.load_session_record(session_id)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc

    def _require_session(self, session_id: str) -> None:
        try:
            self._store.load_session_record(session_id)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc


def _normalize_title(value: str) -> str:
    title = " ".join(value.split())
    if not title:
        raise ValueError("session title cannot be empty")
    if len(title) > _MAX_TITLE_LENGTH:
        raise ValueError(f"session title exceeds {_MAX_TITLE_LENGTH} characters")
    return title


def _branch_title(source_title: str) -> str:
    suffix = " · 分支"
    normalized = _normalize_title(source_title)
    if len(normalized) + len(suffix) <= _MAX_TITLE_LENGTH:
        return f"{normalized}{suffix}"
    return f"{normalized[: _MAX_TITLE_LENGTH - len(suffix)].rstrip()}{suffix}"


def _filter_entries(
    entries: Iterable[SessionIndexEntry],
    query: SessionCatalogQuery,
) -> list[SessionIndexEntry]:
    filtered = [entry for entry in entries if _matches_query(entry, query)]
    if query.sort == "created_desc":
        return sorted(
            filtered,
            key=lambda entry: (entry.created_at, entry.session_id),
            reverse=True,
        )
    if query.sort == "title":
        return sorted(filtered, key=lambda entry: (entry.title.casefold(), entry.session_id))
    return sorted(
        filtered,
        key=lambda entry: (entry.last_activity_at, entry.session_id),
        reverse=True,
    )


def _matches_query(entry: SessionIndexEntry, query: SessionCatalogQuery) -> bool:
    if query.statuses and entry.status not in query.statuses:
        return False
    if query.archived is not None and entry.archived is not query.archived:
        return False
    if query.pinned is not None and entry.pinned is not query.pinned:
        return False
    if query.workspace is not None and entry.workspace != query.workspace:
        return False
    if query.model is not None and entry.current_model != query.model:
        return False
    if query.tags and not query.tags.issubset(entry.tags):
        return False
    if query.health and entry.health not in query.health:
        return False
    if not _inside_range(entry.created_at, query.created_after, query.created_before):
        return False
    if not _inside_range(entry.updated_at, query.updated_after, query.updated_before):
        return False
    if query.text:
        needle = query.text.casefold().strip()
        fields = (
            entry.title,
            entry.last_message_preview,
            entry.workspace or "",
            entry.current_model or "",
            " ".join(entry.tags),
        )
        if needle and not any(needle in field.casefold() for field in fields):
            return False
    return True


def _inside_range(value: datetime, lower: datetime | None, upper: datetime | None) -> bool:
    return (lower is None or value >= lower) and (upper is None or value <= upper)
