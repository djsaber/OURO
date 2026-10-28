from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import ExitStack, closing
from pathlib import Path

from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.resources import (
    ResourceExportResult,
    ResourceListResult,
    ResourceMutationResult,
    ResourceRemoveResult,
    ResourceView,
)
from ouro.application.inputs.files import (
    FileInputProjector,
    PreparedFileInputs,
    with_attachment_read_paths,
)
from ouro.application.inputs.queue import QueuedInputQueue
from ouro.core.base import new_id, utc_now
from ouro.core.content import MediaContent, MediaModality
from ouro.core.errors import (
    PersistenceError,
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
from ouro.resources.media import capture_media
from ouro.sessions import SessionStore

DEFAULT_MAX_RESOURCE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_MODEL_TEXT_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_MODEL_MEDIA_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_MODEL_ATTACHMENTS = 16


class ResourceService:
    """Preparation, Session ownership, and lifecycle of named input files."""

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

    def reserve(self, identity: str | None = None) -> str:
        """Reserve a stable file namespace without creating a Session."""
        identity = identity or new_id("ses")
        with self._sessions.session_mutation(identity):
            if self._session_exists(identity):
                raise SessionCommitConflict("Resource namespace already belongs to a Session")
            return self._store.reserve(identity)

    def prepared_references(self, identity: str) -> list[SessionResourceReference] | None:
        """Return the reserved manifest, or None when no resources were prepared."""
        return self._store.load_prepared(identity)

    def import_prepared_resource(self, identity: str, path: Path) -> ResourceMutationResult:
        with self._sessions.session_mutation(identity):
            if self._session_exists(identity):
                raise SessionCommitConflict("Prepared resources already belong to a Session")
            records = self._store.load_prepared(identity)
            if records is None:
                raise PersistenceError("Resource namespace is not reserved")
            record = self._store.import_file(
                identity,
                path,
                source=ResourceSource(name=path.name),
                media_type=self._store.infer_media_type(path),
                max_bytes=self._max_resource_bytes,
            )
            self._store.save_prepared(
                identity, [*records, SessionResourceReference(**record.model_dump())]
            )
        return ResourceMutationResult(resource=ResourceView.from_record(record), created=True)

    def release_prepared(self, identity: str, *, retain: tuple[str, ...] = ()) -> None:
        """Release abandoned preparation; accepted Sessions always retain their resources."""
        with self._sessions.session_mutation(identity):
            if self._session_exists(identity):
                return
            if not retain:
                self._store.discard_prepared(identity)
                return
            records = self._store.load_prepared(identity)
            if records is None:
                return
            retained = [item for item in records if item.resource_id in retain]
            removed = [item for item in records if item.resource_id not in retain]
            self._store.save_prepared(identity, retained)
            self._store.delete_resources(identity, removed)

    def _session_exists(self, identity: str) -> bool:
        try:
            self._sessions.load_session_record(identity)
        except FileNotFoundError:
            return False
        return True

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
            # A failed acknowledgement can follow metadata publication. Retain
            # the file; a possible orphan is preferable to a dangling confirmed ID.
            self._sessions.attach_resource_reference(
                session_id, SessionResourceReference(**record.model_dump())
            )
        return ResourceMutationResult(
            resource=ResourceView.from_record(record),
            created=True,
            diagnostics=self._index_diagnostics(session_id),
        )

    def _record(self, session_id: str, resource_id: str) -> SessionResourceReference:
        return self._sessions.load_resource_reference(session_id, resource_id)

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
                if not item.internal
            ),
        )

    def show_resource(
        self, session_id: str, resource_id: str, *, verify: bool = True
    ) -> ResourceView:
        with self._sessions.session_read(session_id):
            record = self._record(session_id, resource_id)
            if verify:
                self._store.verify(session_id, record)
            return ResourceView.from_record(record)

    def export_resource(
        self, session_id: str, resource_id: str, destination: str | Path, *, overwrite: bool = False
    ) -> ResourceExportResult:
        with self._sessions.session_read(session_id):
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
            with (
                self._sessions.session_read(session_id),
                self._sessions.session_mutation(session_id),
            ):
                record = self._record(session_id, resource_id)
                session = self._sessions.load_session_record(session_id)
                with closing(self._sessions.iter_messages_before(session_id)) as messages:
                    if any(
                        resource_id in message.referenced_resource_ids for _, message in messages
                    ):
                        raise ResourceInUseError(
                            f"resource {resource_id} is referenced by a canonical message"
                        )
                request = self._sessions.load_latest_request(session_id)
                if (
                    request is not None
                    and resource_id in request[1].manifest.referenced_resource_ids
                ):
                    raise ResourceInUseError(
                        f"resource {resource_id} is referenced by the latest request"
                    )
                if any(
                    resource_id in item.resource_refs
                    for item in QueuedInputQueue(self._sessions).eligible_session(session)
                ):
                    raise ResourceInUseError(
                        f"resource {resource_id} is referenced by a queued input"
                    )
                if self._sessions.tool_outcomes_use_resource(session_id, resource_id):
                    raise ResourceInUseError(
                        f"resource {resource_id} is referenced by a tool Outcome"
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
        # Only selected identities are retained for this preparation.
        selected: dict[tuple[str, str], SessionResourceReference] = {}

        def record(session_id: str, resource_id: str) -> SessionResourceReference:
            key = (session_id, resource_id)
            if key not in selected:
                selected[key] = self._record(session_id, resource_id)
            return selected[key]

        return FileInputProjector(
            store=self._store,
            record=record,
            capture=self._capture_media,
            max_resource_bytes=self._max_resource_bytes,
            max_text_bytes=self._max_model_text_bytes,
            max_media_bytes=self._max_model_media_bytes,
        )

    def _capture_media(self, session_id: str, name: str, media: MediaContent) -> MediaContent:
        """Publish observed workspace bytes before the message can reference them."""

        def publish(record: SessionResourceReference, content: bytes) -> None:
            self._store.import_contents(session_id, ((record, content),))
            self._sessions.attach_resource_reference(session_id, record)

        return capture_media(media, source=ResourceSource(name=name), publish=publish)

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
        with ExitStack() as reads:
            for identity in sorted({message.session_id for message in messages}):
                reads.enter_context(self._sessions.session_read(identity))
            return self._projector().project(
                messages, input_modalities=input_modalities, input_budget_tokens=input_budget_tokens
            )

    def prepare_model_messages(
        self,
        messages: list[MessageRecord],
        *,
        input_modalities: frozenset[MediaModality],
        input_budget_tokens: int | None = None,
    ) -> PreparedFileInputs:
        return self._projector().prepare(
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

    def rebind_input_paths(
        self, messages: list[MessageRecord], records: Iterable[ResourceRecord]
    ) -> list[MessageRecord]:
        """Bind copied scaffolds before publishing the destination Session snapshot."""
        by_id = {record.resource_id: record for record in records}
        return [
            with_attachment_read_paths(
                message,
                (
                    self._store.path_for(message.session_id, by_id[identity])
                    for identity in dict.fromkeys(message.resource_refs)
                ),
            )
            if message.semantic_tag == "attachments"
            else message
            for message in messages
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
            for identity in identities:
                source = self._record(source_session_id, identity)
                self._store.verify(source_session_id, source)
                try:
                    existing = self._record(target_session_id, identity)
                except ResourceNotFoundError:
                    existing = None
                if existing is not None:
                    if (existing.digest, existing.size, existing.media_type) != (
                        source.digest,
                        source.size,
                        source.media_type,
                    ):
                        raise PersistenceError(
                            "resource identity is already bound to other content"
                        )
                    self._store.verify(target_session_id, existing)
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
