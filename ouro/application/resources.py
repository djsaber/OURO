from __future__ import annotations

import base64
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
    ResourceAttachmentError,
    ResourceInUseError,
    ResourceNotFoundError,
    SessionBusyError,
    UnsupportedMediaTypeError,
)
from ouro.core.messages import ImageContent, MessageRecord, TextContent
from ouro.core.resources import ResourceSource, ResourceSourceKind, SessionResourceReference
from ouro.resources import ResourceStore
from ouro.sessions import SessionRepository

DEFAULT_MAX_RESOURCE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_MODEL_TEXT_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_MODEL_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_MODEL_ATTACHMENTS = 16
_TEXT_APPLICATION_TYPES = frozenset({"application/json", "application/xml"})
_IMAGE_MEDIA_TYPES = frozenset({"image/gif", "image/jpeg", "image/png", "image/webp"})


class ResourceService:
    """Application use cases for immutable resources and their Session reachability."""

    def __init__(
        self,
        *,
        store: ResourceStore,
        sessions: SessionRepository,
        max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
        max_model_text_bytes: int = DEFAULT_MAX_MODEL_TEXT_BYTES,
        max_model_image_bytes: int = DEFAULT_MAX_MODEL_IMAGE_BYTES,
        max_model_attachments: int = DEFAULT_MAX_MODEL_ATTACHMENTS,
    ) -> None:
        if (
            min(
                max_resource_bytes,
                max_model_text_bytes,
                max_model_image_bytes,
                max_model_attachments,
            )
            <= 0
        ):
            raise ValueError("resource and model attachment limits must be positive")
        self._store = store
        self._sessions = sessions
        self._max_resource_bytes = max_resource_bytes
        self._max_model_text_bytes = max_model_text_bytes
        self._max_model_image_bytes = max_model_image_bytes
        self._max_model_attachments = max_model_attachments

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
            with self._sessions.session_mutation(session_id):
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
                        path,
                        source=source,
                        media_type=selected_media_type,
                        max_bytes=self._max_resource_bytes,
                    )
                    try:
                        self._sessions.attach_resource_reference(
                            session_id,
                            SessionResourceReference(resource_id=record.resource_id),
                        )
                    except Exception:
                        if created and not self._sessions.is_resource_referenced(
                            record.resource_id
                        ):
                            self._store.delete_resources([record.resource_id])
                        raise
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
        records = [
            self._store.load_record(reference.resource_id)
            for reference in self._sessions.load_resource_references(session_id)
        ]
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
            with self._sessions.session_mutation(session_id):
                self._require_attached(session_id, resource_id)
                snapshot = self._sessions.load_runtime_session(session_id)
                users = [
                    message.message_id
                    for message in snapshot.messages
                    if resource_id in message.resource_refs
                ]
                if users:
                    raise ResourceInUseError(
                        f"resource {resource_id} is referenced by canonical message {users[0]}"
                    )
                self._sessions.remove_resource_reference(session_id, resource_id)
        finally:
            lock.release()
        if not self._sessions.is_resource_referenced(resource_id):
            self._store.delete_resources([resource_id])
        collected = not self._record_exists(resource_id)
        return ResourceRemoveResult(
            session_id=session_id,
            resource_id=resource_id,
            content_collected=collected,
            diagnostics=self._index_diagnostics(session_id, resource_id),
        )

    def validate_references(self, session_id: str, resource_ids: Iterable[str]) -> None:
        attached = {
            reference.resource_id
            for reference in self._sessions.load_resource_references(session_id)
        }
        for resource_id in dict.fromkeys(resource_ids):
            if resource_id not in attached:
                raise ResourceNotFoundError(
                    f"resource {resource_id} is not attached to session {session_id}"
                )
            self._store.load_record(resource_id, verify=True)

    def validate_model_references(
        self,
        session_id: str,
        resource_ids: Iterable[str],
        *,
        supports_vision: bool,
        message_role: str = "user",
    ) -> tuple[str, ...]:
        unique_ids = self._validated_model_ids(session_id, resource_ids)
        for resource_id in unique_ids:
            self._model_blocks(
                resource_id,
                supports_vision=supports_vision,
                message_role=message_role,
            )
        return unique_ids

    def project_model_messages(
        self,
        messages: list[MessageRecord],
        *,
        supports_vision: bool,
    ) -> list[MessageRecord]:
        """Materialize canonical references into request-local content blocks."""

        projected: list[MessageRecord] = []
        for message in messages:
            if not message.resource_refs:
                projected.append(message)
                continue
            unique_ids = self._validated_model_ids(
                message.session_id,
                message.resource_refs,
            )
            content = list(message.content)
            for resource_id in unique_ids:
                content.extend(
                    self._model_blocks(
                        resource_id,
                        supports_vision=supports_vision,
                        message_role=message.role,
                    )
                )
            projected.append(message.model_copy(update={"content": content}))
        return projected

    def _validated_model_ids(
        self,
        session_id: str,
        resource_ids: Iterable[str],
    ) -> tuple[str, ...]:
        unique_ids = tuple(dict.fromkeys(resource_ids))
        if len(unique_ids) > self._max_model_attachments:
            raise ResourceAttachmentError(
                "model input has too many attachments: "
                f"{len(unique_ids)} > {self._max_model_attachments}"
            )
        self.validate_references(session_id, unique_ids)
        return unique_ids

    def _model_blocks(
        self,
        resource_id: str,
        *,
        supports_vision: bool,
        message_role: str,
    ) -> list[TextContent | ImageContent]:
        record = self._store.load_record(resource_id)
        media_type = record.media_type.lower()
        label = (
            f"\n\n[Attached resource: {record.source.name} "
            f"({record.resource_id}, {record.media_type})]"
        )
        if media_type.startswith("text/") or media_type in _TEXT_APPLICATION_TYPES:
            raw = self._load_model_content(
                resource_id,
                max_bytes=self._max_model_text_bytes,
            )
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ResourceAttachmentError(
                    f"text resource is not valid UTF-8: {resource_id}"
                ) from exc
            return [TextContent(text=f"{label}\n{text}")]
        if media_type in _IMAGE_MEDIA_TYPES:
            if message_role != "user":
                raise ResourceAttachmentError(
                    "image attachments are only supported on user messages"
                )
            if not supports_vision:
                raise ResourceAttachmentError(
                    f"selected model endpoint does not support image attachment {resource_id}"
                )
            raw = self._load_model_content(
                resource_id,
                max_bytes=self._max_model_image_bytes,
            )
            encoded = base64.b64encode(raw).decode("ascii")
            return [
                TextContent(text=label),
                ImageContent(
                    uri=f"data:{record.media_type};base64,{encoded}",
                    media_type=record.media_type,
                ),
            ]
        raise UnsupportedMediaTypeError(
            f"resource media type is not supported for model input: {record.media_type}"
        )

    def _load_model_content(self, resource_id: str, *, max_bytes: int) -> bytes:
        try:
            _, content = self._store.load_content(resource_id, max_bytes=max_bytes)
        except ValueError as exc:
            raise ResourceAttachmentError(str(exc)) from exc
        return content

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
        candidates = [
            resource_id
            for resource_id in dict.fromkeys(resource_ids)
            if not self._sessions.is_resource_referenced(resource_id)
        ]
        return tuple(self._store.delete_resources(candidates))

    def session_resource_ids(self, session_id: str) -> tuple[str, ...]:
        return tuple(
            reference.resource_id
            for reference in self._sessions.load_resource_references(session_id)
        )

    def _require_attached(self, session_id: str, resource_id: str) -> None:
        self._sessions.load_session_record(session_id)
        if not any(
            reference.resource_id == resource_id
            for reference in self._sessions.load_resource_references(session_id)
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
