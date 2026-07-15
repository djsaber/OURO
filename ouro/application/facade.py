from __future__ import annotations

from typing import TYPE_CHECKING

from ouro.application.resource_models import (
    ResourceExportResult,
    ResourceListResult,
    ResourceMutationResult,
    ResourceRemoveResult,
    ResourceView,
)
from ouro.application.resources import ResourceService
from ouro.application.session_models import (
    SessionBranchResult,
    SessionCatalogQuery,
    SessionCatalogResult,
    SessionDetail,
    SessionEventPage,
    SessionMessagePage,
    SessionMutationResult,
    SessionReplay,
    SessionTrashResult,
)
from ouro.application.sessions import SessionService, SessionTitleGenerator
from ouro.application.usage import UsageService
from ouro.application.usage_models import (
    UsageAuditLog,
    UsageEntryList,
    UsageGroupBy,
    UsageMutationResult,
    UsageSummary,
)
from ouro.core.approvals import SessionApprovalMode
from ouro.core.billing import ProviderUsageEntry, UsageFilter
from ouro.core.compaction import CompactionCommandResult
from ouro.core.errors import OuroError
from ouro.core.results import RunResult
from ouro.sessions import SessionStore

if TYPE_CHECKING:
    from ouro.application.queued_inputs import QueuedInputMode
    from ouro.application.runtime import RuntimeService


class ApplicationFacade:
    """In-process command/query surface shared by every OURO host."""

    def __init__(
        self,
        *,
        sessions: SessionService,
        runtime: RuntimeService | None = None,
        resources: ResourceService | None = None,
        usage: UsageService | None = None,
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._resources = resources
        self._usage = usage

    @classmethod
    def from_runtime(cls, runtime: RuntimeService) -> ApplicationFacade:
        return cls(
            sessions=runtime.sessions,
            runtime=runtime,
            resources=runtime.resources,
            usage=runtime.usage,
        )

    @classmethod
    def for_sessions(cls, store: SessionStore) -> ApplicationFacade:
        return cls(sessions=SessionService(store))

    @classmethod
    def with_resources(
        cls,
        store: SessionStore,
        resources: ResourceService,
    ) -> ApplicationFacade:
        return cls(
            sessions=SessionService(store, resources=resources),
            resources=resources,
        )

    @classmethod
    def with_usage(cls, store: SessionStore, usage: UsageService) -> ApplicationFacade:
        return cls(sessions=SessionService(store), usage=usage)

    def create_session(
        self,
        *,
        title: str = "New session",
        workspace: str | None = None,
        current_model: str | None = None,
        approval_mode: SessionApprovalMode = "inherit",
    ) -> SessionMutationResult:
        return self._sessions.create(
            title=title,
            workspace=workspace,
            current_model=current_model,
            approval_mode=approval_mode,
        )

    def open_session(self, session_id: str, *, recover: bool = True) -> SessionDetail:
        return self._sessions.open(session_id, recover=recover)

    def session_messages(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> SessionMessagePage:
        return self._sessions.message_page(session_id, cursor=cursor, limit=limit)

    def session_events(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        run_id: str | None = None,
    ) -> SessionEventPage:
        return self._sessions.event_page(
            session_id,
            cursor=cursor,
            limit=limit,
            run_id=run_id,
        )

    def session_catalog(self, query: SessionCatalogQuery | None = None) -> SessionCatalogResult:
        return self._sessions.catalog(query)

    def rename_session(self, session_id: str, title: str) -> SessionMutationResult:
        return self._sessions.rename(session_id, title)

    def archive_session(self, session_id: str, *, archived: bool = True) -> SessionMutationResult:
        return self._sessions.set_archived(session_id, archived)

    def pin_session(self, session_id: str, *, pinned: bool = True) -> SessionMutationResult:
        return self._sessions.set_pinned(session_id, pinned)

    def set_session_approval_mode(
        self,
        session_id: str,
        approval_mode: SessionApprovalMode,
    ) -> SessionMutationResult:
        return self._sessions.set_approval_mode(session_id, approval_mode)

    async def generate_session_title(
        self,
        session_id: str,
        generator: SessionTitleGenerator,
    ) -> SessionMutationResult:
        return await self._sessions.generate_title(session_id, generator)

    def branch_session(self, session_id: str, message_id: str) -> SessionBranchResult:
        return self._sessions.branch(session_id, message_id)

    def replay_session(self, session_id: str, *, run_id: str | None = None) -> SessionReplay:
        return self._sessions.replay(session_id, run_id=run_id)

    def delete_session(self, session_id: str) -> SessionTrashResult:
        return self._sessions.trash(session_id)

    def import_resource(
        self,
        session_id: str,
        source_path: str,
        *,
        media_type: str | None = None,
    ) -> ResourceMutationResult:
        return self._require_resources().import_resource(
            session_id,
            source_path,
            media_type=media_type,
        )

    def list_resources(self, session_id: str) -> ResourceListResult:
        return self._require_resources().list_resources(session_id)

    def show_resource(self, session_id: str, resource_id: str) -> ResourceView:
        return self._require_resources().show_resource(session_id, resource_id)

    def export_resource(
        self,
        session_id: str,
        resource_id: str,
        destination: str,
        *,
        overwrite: bool = False,
    ) -> ResourceExportResult:
        return self._require_resources().export_resource(
            session_id,
            resource_id,
            destination,
            overwrite=overwrite,
        )

    def remove_resource(self, session_id: str, resource_id: str) -> ResourceRemoveResult:
        return self._require_resources().remove_resource(session_id, resource_id)

    async def start_run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        title: str | None = None,
        initial_model: str | None = None,
        queue_mode: QueuedInputMode = "follow_up",
        approval_mode: SessionApprovalMode | None = None,
        resource_refs: list[str] | tuple[str, ...] | None = None,
    ) -> RunResult:
        return await self._require_runtime().run(
            prompt,
            session_id=session_id,
            title=title,
            initial_model=initial_model,
            queue_mode=queue_mode,
            approval_mode=approval_mode,
            resource_refs=resource_refs,
        )

    async def abort_run(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        reason: str = "User aborted",
    ) -> RunResult:
        return await self._require_runtime().abort_run(
            session_id=session_id,
            run_id=run_id,
            reason=reason,
        )

    async def compact_session(self, session_id: str) -> CompactionCommandResult:
        return await self._require_runtime().compact(session_id=session_id)

    def usage_summary(
        self,
        usage_filter: UsageFilter | None = None,
        *,
        group_by: UsageGroupBy | None = None,
    ) -> UsageSummary:
        return self._require_usage().summarize(usage_filter, group_by=group_by)

    def usage_entries(
        self,
        usage_filter: UsageFilter | None = None,
        *,
        limit: int = 100,
    ) -> UsageEntryList:
        return self._require_usage().list_entries(usage_filter, limit=limit)

    def usage_entry(self, entry_id: str) -> ProviderUsageEntry:
        return self._require_usage().get_entry(entry_id)

    def replace_usage_entry(
        self,
        entry_id: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        estimated_cost: float | None = None,
        clear_estimated_cost: bool = False,
        note: str,
    ) -> UsageMutationResult:
        return self._require_usage().replace_entry(
            entry_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            estimated_cost=estimated_cost,
            clear_estimated_cost=clear_estimated_cost,
            note=note,
        )

    def reset_usage(
        self,
        usage_filter: UsageFilter | None = None,
        *,
        note: str,
    ) -> UsageMutationResult:
        return self._require_usage().reset(usage_filter, note=note)

    def usage_audit(self, *, limit: int = 100) -> UsageAuditLog:
        return self._require_usage().audit(limit=limit)

    def _require_runtime(self) -> RuntimeService:
        if self._runtime is None:
            raise OuroError("this application facade has no runtime commands configured")
        return self._runtime

    def _require_resources(self) -> ResourceService:
        if self._resources is None:
            raise OuroError("this application facade has no resource commands configured")
        return self._resources

    def _require_usage(self) -> UsageService:
        if self._usage is None:
            raise OuroError("this application facade has no usage commands configured")
        return self._usage
