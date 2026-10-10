from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from pathlib import Path

from ouro.application.dto import Diagnostic
from ouro.application.file_inputs import FileInputProjector
from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.resource_models import (
    ResourceExportResult,
    ResourceListResult,
    ResourceMutationResult,
    ResourceRemoveResult,
    ResourceView,
)
from ouro.core.base import new_id, utc_now
from ouro.core.content import MediaModality
from ouro.core.errors import (
    ResourceAttachmentError,
    ResourceInUseError,
    ResourceNotFoundError,
    SessionBusyError,
)
from ouro.core.input_content import InputFile, InputFileReference
from ouro.core.messages import MessageRecord
from ouro.core.resources import (
    ResourceRecord,
    ResourceSource,
    ResourceSourceKind,
    SessionResourceReference,
)
from ouro.core.session_commits import SessionCommitConflict
from ouro.resources import ResourceStore
from ouro.sessions import SessionStore

DEFAULT_MAX_RESOURCE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_MODEL_TEXT_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_MODEL_MEDIA_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_MODEL_ATTACHMENTS = 16


class ResourceService:
    """Session ownership and lifecycle of named input files."""

    def __init__(
        self,
        *,
        store: ResourceStore,
        sessions: SessionStore,
        max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
        max_model_text_bytes: int = DEFAULT_MAX_MODEL_TEXT_BYTES,
        max_model_media_bytes: int = DEFAULT_MAX_MODEL_MEDIA_BYTES,
        max_model_attachments: int = DEFAULT_MAX_MODEL_ATTACHMENTS,
    ) -> None:
        if (
            min(
                max_resource_bytes,
                max_model_text_bytes,
                max_model_media_bytes,
                max_model_attachments,
            )
            <= 0
        ):
            raise ValueError("resource and model attachment limits must be positive")
        self._store, self._sessions = store, sessions
        self._max_resource_bytes = max_resource_bytes
        self._max_model_text_bytes = max_model_text_bytes
        self._max_model_media_bytes = max_model_media_bytes
        self._max_model_attachments = max_model_attachments

    def import_resource(
        self,
        session_id: str,
        source_path: str | Path,
        *,
        media_type: str | None = None,
        source_kind: ResourceSourceKind = "import",
        source_id: str | None = None,
        history_epoch: int | None = None,
    ) -> ResourceMutationResult:
        with self._sessions.session_mutation(session_id):
            session = self._sessions.load_session_record(session_id)
            if history_epoch is not None and history_epoch != session.history_epoch:
                raise SessionCommitConflict("Attachment belongs to withdrawn Session history")
            path = Path(source_path)
            record = self._store.import_file(
                session_id,
                path,
                source=ResourceSource(kind=source_kind, name=path.name, source_id=source_id),
                media_type=media_type or self._store.infer_media_type(path),
                max_bytes=self._max_resource_bytes,
            )
            try:
                self._sessions.attach_resource_reference(
                    session_id, SessionResourceReference(**record.model_dump())
                )
            except Exception:
                self._store.delete_resources(session_id, (record,))
                raise
        return ResourceMutationResult(
            resource=ResourceView.from_record(record),
            created=True,
            diagnostics=self._index_diagnostics(session_id),
        )

    def _record(self, session_id: str, resource_id: str) -> SessionResourceReference:
        self._sessions.load_session_record(session_id)
        record = next(
            (
                item
                for item in self._sessions.load_resource_references(session_id)
                if item.resource_id == resource_id
            ),
            None,
        )
        if record is None:
            raise ResourceNotFoundError(
                f"resource {resource_id} is not attached to session {session_id}"
            )
        return record

    def directory_for(self, session_id: str) -> Path:
        return self._store.directory_for(session_id)

    def resource_path(self, session_id: str, resource_id: str) -> Path:
        return self._store.path_for(session_id, self._record(session_id, resource_id))

    def session_resource_ids(self, session_id: str) -> tuple[str, ...]:
        return tuple(
            item.resource_id for item in self._sessions.load_resource_references(session_id)
        )

    def list_resources(self, session_id: str) -> ResourceListResult:
        self._sessions.load_session_record(session_id)
        return ResourceListResult(
            session_id=session_id,
            resources=tuple(
                ResourceView.from_record(item)
                for item in self._sessions.load_resource_references(session_id)
            ),
        )

    def show_resource(
        self, session_id: str, resource_id: str, *, verify: bool = True
    ) -> ResourceView:
        record = self._record(session_id, resource_id)
        if verify:
            self._store.verify(session_id, record)
        return ResourceView.from_record(record)

    def export_resource(
        self, session_id: str, resource_id: str, destination: str | Path, *, overwrite: bool = False
    ) -> ResourceExportResult:
        record = self._record(session_id, resource_id)
        exported = self._store.export_file(
            session_id, record, Path(destination), overwrite=overwrite
        )
        return ResourceExportResult(resource=ResourceView.from_record(record), destination=exported)

    def remove_resource(self, session_id: str, resource_id: str) -> ResourceRemoveResult:
        lock = self._sessions.acquire_session_lock(session_id, new_id("resource"))
        if lock is None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        try:
            with self._sessions.session_mutation(session_id):
                record = self._record(session_id, resource_id)
                snapshot = self._sessions.load_runtime_session(session_id)
                if any(resource_id in message.resource_refs for message in snapshot.messages):
                    raise ResourceInUseError(
                        f"resource {resource_id} is referenced by a canonical message"
                    )
                if any(
                    resource_id in item.resource_refs
                    for item in QueuedInputQueue(self._sessions).eligible(snapshot)
                ):
                    raise ResourceInUseError(
                        f"resource {resource_id} is referenced by a queued input"
                    )
                self._sessions.remove_resource_reference(session_id, resource_id)
                try:
                    self._store.delete_resources(session_id, (record,))
                except Exception:
                    self._sessions.attach_resource_reference(session_id, record)
                    raise
        finally:
            lock.release()
        return ResourceRemoveResult(
            session_id=session_id,
            resource_id=resource_id,
            content_collected=True,
            diagnostics=self._index_diagnostics(session_id),
        )

    def validate_references(self, session_id: str, resource_ids: Iterable[str]) -> None:
        for identity in dict.fromkeys(resource_ids):
            self._store.verify(session_id, self._record(session_id, identity))

    def validate_model_references(
        self, session_id: str, resource_ids: Iterable[str]
    ) -> tuple[str, ...]:
        identities = tuple(dict.fromkeys(resource_ids))
        if len(identities) > self._max_model_attachments:
            raise ResourceAttachmentError("Too many files in one input")
        for identity in identities:
            self._record(session_id, identity)
        return identities

    def validate_file_inputs(self, items: tuple[InputFile, ...], workspace: str | None) -> None:
        if len(items) > self._max_model_attachments:
            raise ResourceAttachmentError("Too many files in one input")
        for item in items:
            if isinstance(item, InputFileReference) and item.workspace is not None:
                if workspace is None or Path(item.workspace).resolve() != Path(workspace).resolve():
                    raise ResourceAttachmentError(
                        "The referenced file belongs to a different workspace; select it again"
                    )

    def _projector(self) -> FileInputProjector:
        # One manifest read per Session and projection, with no cache across requests.
        manifests: dict[str, dict[str, SessionResourceReference]] = {}

        def record(session_id: str, resource_id: str) -> SessionResourceReference:
            if session_id not in manifests:
                self._sessions.load_session_record(session_id)
                manifests[session_id] = {
                    item.resource_id: item
                    for item in self._sessions.load_resource_references(session_id)
                }
            try:
                return manifests[session_id][resource_id]
            except KeyError as exc:
                raise ResourceNotFoundError(
                    f"resource {resource_id} is not attached to session {session_id}"
                ) from exc

        return FileInputProjector(
            store=self._store,
            record=record,
            max_resource_bytes=self._max_resource_bytes,
            max_text_bytes=self._max_model_text_bytes,
            max_media_bytes=self._max_model_media_bytes,
        )

    def prepare_input(
        self,
        message: MessageRecord,
        *,
        input_modalities: frozenset[MediaModality],
        workspace: str | None,
        workspace_only: bool,
    ) -> MessageRecord:
        return self._projector().prepare_input(
            message,
            input_modalities=input_modalities,
            workspace=workspace,
            workspace_only=workspace_only,
        )

    def project_model_messages(
        self,
        messages: list[MessageRecord],
        *,
        input_modalities: frozenset[MediaModality],
        input_budget_tokens: int | None = None,
    ) -> list[MessageRecord]:
        return self._projector().project(
            messages, input_modalities=input_modalities, input_budget_tokens=input_budget_tokens
        )

    def branch_references(
        self, source_session_id: str, resource_ids: Iterable[str]
    ) -> list[SessionResourceReference]:
        resource_ids = tuple(resource_ids)
        self.validate_references(source_session_id, resource_ids)
        return [
            self._record(source_session_id, identity).model_copy(
                update={"attached_at": utc_now(), "copied_from_session_id": source_session_id}
            )
            for identity in dict.fromkeys(resource_ids)
        ]

    def contents_for(
        self, session_id: str, records: Iterable[ResourceRecord]
    ) -> Iterator[tuple[ResourceRecord, bytes]]:
        for record in records:
            yield (
                record,
                self._store.load_content(session_id, record, max_bytes=self._max_resource_bytes),
            )

    def copy_references(
        self, source_session_id: str, target_session_id: str, resource_ids: Iterable[str]
    ) -> tuple[str, ...]:
        identities = tuple(dict.fromkeys(resource_ids))
        if source_session_id == target_session_id:
            self.validate_references(source_session_id, identities)
            return identities
        # Stable lock order prevents opposite-direction transfers from deadlocking.
        with ExitStack() as locks:
            for identity in sorted({source_session_id, target_session_id}):
                locks.enter_context(self._sessions.session_mutation(identity))
            existing = {
                item.resource_id
                for item in self._sessions.load_resource_references(target_session_id)
            }
            for identity in identities:
                source = self._record(source_session_id, identity)
                self._store.verify(source_session_id, source)
                if identity in existing:
                    self._store.verify(target_session_id, self._record(target_session_id, identity))
                    continue
                copied = self._store.import_file(
                    target_session_id,
                    self._store.path_for(source_session_id, source),
                    source=source.source,
                    media_type=source.media_type,
                    max_bytes=self._max_resource_bytes,
                ).model_copy(update={"resource_id": identity})
                try:
                    self._sessions.attach_resource_reference(
                        target_session_id,
                        SessionResourceReference(
                            **copied.model_dump(), copied_from_session_id=source_session_id
                        ),
                    )
                except Exception:
                    self._store.delete_resources(target_session_id, (copied,))
                    raise
        return identities

    def _index_diagnostics(self, session_id: str) -> tuple[Diagnostic, ...]:
        error = self._sessions.refresh_catalog(session_id)
        if error is None:
            return ()
        return (
            Diagnostic(
                code="session_index_update_failed",
                severity="warning",
                component="resources",
                message=error,
                session_id=session_id,
                recoverable=True,
            ),
        )
