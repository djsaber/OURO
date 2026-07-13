from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ouro.application.dto import Diagnostic
from ouro.application.resource_models import (
    ResourceExportResult,
    ResourceListResult,
    ResourceMutationResult,
    ResourceRemoveResult,
    ResourceView,
)
from ouro.core.base import new_id
from ouro.core.errors import (
    ResourceInUseError,
    ResourceNotFoundError,
    SessionBusyError,
    UnsupportedMediaTypeError,
)
from ouro.core.resources import ResourceSource, ResourceSourceKind, SessionResourceReference
from ouro.resources import ResourceStore
from ouro.sessions import SessionStore, session_mutation

DEFAULT_MAX_RESOURCE_BYTES = 100 * 1024 * 1024


class ResourceService:
    """Application use cases for immutable resources and their Session reachability."""

    def __init__(
        self,
        *,
        store: ResourceStore,
        sessions: SessionStore,
        max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
    ) -> None:
        if max_resource_bytes <= 0:
            raise ValueError("max_resource_bytes must be positive")
        self._store = store
        self._sessions = sessions
        self._max_resource_bytes = max_resource_bytes

    def import_resource(
        self,
        session_id: str,
        source_path: str | Path,
        *,
        media_type: str | None = None,
        source_kind: ResourceSourceKind = "import",
        source_id: str | None = None,
    ) -> ResourceMutationResult:
        lock = self._sessions.acquire_session_lock(session_id, new_id("resource"))
        if lock is None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        try:
            with session_mutation(self._sessions, session_id):
                self._sessions.load_session_record(session_id)
                path = Path(source_path)
                selected_media_type = media_type or self._store.infer_media_type(path)
                source = ResourceSource(
                    kind=source_kind,
                    name=path.name,
                    source_id=source_id,
                )
                try:
                    record, created = self._store.import_file(
                        session_id,
                        path,
                        source=source,
                        media_type=selected_media_type,
                        max_bytes=self._max_resource_bytes,
                    )
                except ValueError as exc:
                    if "media_type" in str(exc):
                        raise UnsupportedMediaTypeError(str(exc)) from exc
                    raise
        finally:
            lock.release()
        return ResourceMutationResult(
            resource=ResourceView.from_record(record),
            created=created,
            diagnostics=self._index_diagnostics(session_id, record.resource_id),
        )

    def list_resources(self, session_id: str) -> ResourceListResult:
        self._sessions.load_session_record(session_id)
        records = self._store.list_session_records(session_id)
        return ResourceListResult(
            session_id=session_id,
            resources=tuple(ResourceView.from_record(record) for record in records),
        )

    def show_resource(
        self,
        session_id: str,
        resource_id: str,
        *,
        verify: bool = True,
    ) -> ResourceView:
        self._require_attached(session_id, resource_id)
        return ResourceView.from_record(self._store.load_record(resource_id, verify=verify))

    def export_resource(
        self,
        session_id: str,
        resource_id: str,
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> ResourceExportResult:
        resource = self.show_resource(session_id, resource_id, verify=True)
        exported = self._store.export_file(resource_id, Path(destination), overwrite=overwrite)
        return ResourceExportResult(resource=resource, destination=exported)

    def remove_resource(self, session_id: str, resource_id: str) -> ResourceRemoveResult:
        lock = self._sessions.acquire_session_lock(session_id, new_id("resource"))
        if lock is None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        try:
            with session_mutation(self._sessions, session_id):
                self._require_attached(session_id, resource_id)
                snapshot = self._sessions.load_session(session_id, recover=False)
                users = [
                    message.message_id
                    for message in snapshot.messages
                    if resource_id in message.resource_refs
                ]
                if users:
                    raise ResourceInUseError(
                        f"resource {resource_id} is referenced by canonical message {users[0]}"
                    )
                self._store.remove_session_reference(session_id, resource_id)
        finally:
            lock.release()
        collected = not self._record_exists(resource_id)
        return ResourceRemoveResult(
            session_id=session_id,
            resource_id=resource_id,
            content_collected=collected,
            diagnostics=self._index_diagnostics(session_id, resource_id),
        )

    def validate_references(self, session_id: str, resource_ids: Iterable[str]) -> None:
        attached = {
            reference.resource_id for reference in self._store.load_session_references(session_id)
        }
        for resource_id in dict.fromkeys(resource_ids):
            if resource_id not in attached:
                raise ResourceNotFoundError(
                    f"resource {resource_id} is not attached to session {session_id}"
                )
            self._store.load_record(resource_id, verify=True)

    def branch_references(
        self,
        source_session_id: str,
        target_session_id: str,
        resource_ids: Iterable[str],
    ) -> list[SessionResourceReference]:
        unique_ids = list(dict.fromkeys(resource_ids))
        self.validate_references(source_session_id, unique_ids)
        return [
            SessionResourceReference(
                resource_id=resource_id,
                copied_from_session_id=source_session_id,
            )
            for resource_id in unique_ids
        ]

    def collect_after_session_trash(self, resource_ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(self._store.collect_unreferenced(resource_ids))

    def session_resource_ids(self, session_id: str) -> tuple[str, ...]:
        return tuple(
            reference.resource_id for reference in self._store.load_session_references(session_id)
        )

    def _require_attached(self, session_id: str, resource_id: str) -> None:
        self._sessions.load_session_record(session_id)
        if not any(
            reference.resource_id == resource_id
            for reference in self._store.load_session_references(session_id)
        ):
            raise ResourceNotFoundError(
                f"resource {resource_id} is not attached to session {session_id}"
            )

    def _record_exists(self, resource_id: str) -> bool:
        try:
            self._store.load_record(resource_id)
        except ResourceNotFoundError:
            return False
        return True

    def _index_diagnostics(
        self,
        session_id: str,
        resource_id: str,
    ) -> tuple[Diagnostic, ...]:
        error = self._store.try_update_index()
        if error is None:
            return ()
        return (
            Diagnostic(
                code="resource_index_refresh_failed",
                severity="warning",
                component="resources.index",
                message=error,
                session_id=session_id,
                resource_id=resource_id,
                recoverable=True,
                suggested_action="Rebuild the resource index.",
            ),
        )
