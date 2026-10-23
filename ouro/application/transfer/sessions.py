from __future__ import annotations

import hashlib
from contextlib import ExitStack

from pydantic import ConfigDict, ValidationError

from ouro.application.contracts.base import ApplicationDTO, Diagnostic
from ouro.application.inputs.queue import QUEUED_INPUTS_KEY, QueuedInput
from ouro.core.base import OuroModel
from ouro.core.errors import SessionBusyError
from ouro.core.input_content import InputAttachment, InputFileReference
from ouro.core.media_context import MEDIA_CONTEXT_KEY
from ouro.core.resources import ResourceRecord
from ouro.core.state import ACTIVE_RUN_STATUSES
from ouro.resources.base import ResourceStore
from ouro.sessions.base import SessionStore
from ouro.sessions.portable import PortableSession, SessionBundle
from ouro.transfer.archive import (
    MAX_ARCHIVE_BYTES,
    ArchiveIndex,
    ArchiveManifest,
    ArchiveSelectionItem,
    DataArchive,
    DataTransferError,
)


class SessionArchiveData(OuroModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    bundle: SessionBundle


class SessionImportResult(ApplicationDTO):
    count: int
    skipped: int = 0
    replaced: int = 0
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionTransferService:
    """Transfer inactive Session trees and their immutable resource closure."""

    def __init__(self, sessions: SessionStore, resources: ResourceStore) -> None:
        self.sessions = sessions
        self.resources = resources

    def export(self, identities: tuple[str, ...]) -> DataArchive:
        records = self.sessions.list_sessions()
        known = {record.session_id: record for record in records}
        missing = set(identities) - known.keys()
        if missing:
            raise DataTransferError(f"sessions not found: {', '.join(sorted(missing))}")
        roots = (
            {known[identity].agent_root_session_id for identity in identities}
            if identities
            else {record.agent_root_session_id for record in records}
        )
        with ExitStack() as locks:
            # Root execution ownership prevents new child dispatch while finding
            # and freezing the full tree. No runtime recovery is run during export.
            for identity in sorted(roots):
                lock = self.sessions.acquire_session_lock(identity, "data-export")
                if lock is None:
                    raise SessionBusyError(f"stop the running Session before export: {identity}")
                locks.callback(lock.release)
            records = [
                record
                for record in self.sessions.list_sessions()
                if record.agent_root_session_id in roots
            ]
            for record in sorted(records, key=lambda item: item.session_id):
                if record.session_id not in roots:
                    lock = self.sessions.acquire_session_lock(record.session_id, "data-export")
                    if lock is None:
                        raise SessionBusyError("stop running child Sessions before export")
                    locks.callback(lock.release)
                locks.enter_context(self.sessions.session_mutation(record.session_id))
            try:
                bundle = SessionBundle(
                    sessions=[
                        PortableSession(
                            snapshot=self.sessions.load_session(record.session_id),
                            resources=self.sessions.load_resource_references(record.session_id),
                            latest_context=self.sessions.load_latest_context(record.session_id),
                        )
                        for record in records
                    ],
                    delegations=[
                        delegation
                        for identity in sorted(roots)
                        for delegation in self.sessions.list_delegations(identity)
                    ],
                )
            except ValidationError as exc:
                raise DataTransferError(
                    "Sessions must be inactive and consistent before export"
                ) from exc
            files: dict[str, bytes] = {}
            size = 0
            for name, (session_id, resource) in _resource_files(bundle).items():
                content = self.resources.load_content(
                    session_id, resource, max_bytes=MAX_ARCHIVE_BYTES
                )
                size += len(content)
                if size > MAX_ARCHIVE_BYTES:
                    raise DataTransferError("Session resources exceed the archive size limit")
                files[name] = content
        data = SessionArchiveData(bundle=bundle)
        manifest = ArchiveManifest(kind="sessions", data=data.model_dump(mode="json"))
        return DataArchive(manifest, files, session_archive_index(manifest))

    def import_data(
        self,
        archive: DataArchive,
        identities: tuple[str, ...] = (),
        *,
        replace: bool = False,
    ) -> SessionImportResult:
        try:
            data = SessionArchiveData.model_validate(archive.manifest.data)
        except ValidationError as exc:
            raise DataTransferError("invalid or unsupported Session archive data") from exc
        if archive.index is not None and archive.index != session_archive_index(archive.manifest):
            raise DataTransferError("archive index does not match its manifest")
        resources = _resource_files(data.bundle)
        if set(archive.files) != set(resources):
            raise DataTransferError("Session archive resource files do not match its manifest")
        for name, (_session_id, record) in resources.items():
            content = archive.files[name]
            if (
                len(content) != record.size
                or f"sha256:{hashlib.sha256(content).hexdigest()}" != record.digest
            ):
                raise DataTransferError("Session archive resource integrity check failed")
        if identities:
            records = {item.snapshot.session.session_id: item for item in data.bundle.sessions}
            missing = set(identities) - records.keys()
            if missing:
                raise DataTransferError(
                    f"sessions not found in archive: {', '.join(sorted(missing))}"
                )
            roots = {
                records[identity].snapshot.session.agent_root_session_id for identity in identities
            }
            data.bundle = _select_roots(data.bundle, roots)
        return self._publish_import(data, archive, replace=replace)

    def _publish_import(
        self, data: SessionArchiveData, archive: DataArchive, *, replace: bool
    ) -> SessionImportResult:
        bundle = data.bundle
        selected_count = len(bundle.sessions)
        existing = {record.session_id: record for record in self.sessions.list_sessions()}
        if not replace:
            bundle = _skip_existing_trees(bundle, set(existing))
        if not bundle.sessions:
            return SessionImportResult(count=0, skipped=selected_count)
        with ExitStack() as locks:
            roots = {item.snapshot.session.agent_root_session_id for item in bundle.sessions}
            for identity in sorted(roots):
                lock = self.sessions.acquire_session_lock(identity, "data-import")
                if lock is None:
                    raise SessionBusyError(f"stop the running Session before import: {identity}")
                locks.callback(lock.release)
            # Recheck after execution ownership: a concurrent import may have
            # published a tree since the initial skip decision.
            existing = {record.session_id: record for record in self.sessions.list_sessions()}
            if not replace:
                bundle = _skip_existing_trees(bundle, set(existing))
            roots = {item.snapshot.session.agent_root_session_id for item in bundle.sessions}
            incoming = {
                item.snapshot.session.session_id: item.snapshot.session for item in bundle.sessions
            }
            for identity in incoming.keys() & existing.keys():
                if (
                    incoming[identity].agent_root_session_id
                    != existing[identity].agent_root_session_id
                    or incoming[identity].session_kind != existing[identity].session_kind
                ):
                    raise DataTransferError("Session identity belongs to a different agent tree")
            replaced_ids = {
                identity
                for identity, record in existing.items()
                if replace and record.agent_root_session_id in roots
            }
            for identity in sorted(incoming.keys() | replaced_ids):
                if identity not in roots:
                    lock = self.sessions.acquire_session_lock(identity, "data-import")
                    if lock is None:
                        raise SessionBusyError(
                            f"stop the running child Session before import: {identity}"
                        )
                    locks.callback(lock.release)
                locks.enter_context(self.sessions.session_mutation(identity))
            for identity in sorted(replaced_ids):
                record = self.sessions.load_session_record(identity)
                if (
                    record.active_run_id is not None
                    or record.status not in {"idle", "failed"}
                    or any(
                        run.status in ACTIVE_RUN_STATUSES
                        for run in self.sessions.load_runs(identity)
                    )
                ):
                    raise SessionBusyError(
                        f"stop the active Session before replacement: {identity}"
                    )
            if bundle.sessions:
                # Import starts a new media projection phase; the saved request remains viewable.
                for item in bundle.sessions:
                    if item.latest_context is not None:
                        metadata = dict(item.latest_context.context_metadata)
                        metadata.pop(MEDIA_CONTEXT_KEY, None)
                        item.latest_context = item.latest_context.model_copy(
                            update={"context_metadata": metadata}
                        )
                contents: dict[str, dict[str, bytes]] = {}
                for name, (session_id, resource) in _resource_files(bundle).items():
                    contents.setdefault(session_id, {})[resource.filename] = archive.files[name]
                self.sessions.import_bundle(
                    bundle,
                    replace_session_ids=tuple(sorted(replaced_ids)),
                    resource_contents=contents,
                )
        return SessionImportResult(
            count=len(bundle.sessions),
            skipped=selected_count - len(bundle.sessions),
            replaced=len(incoming.keys() & replaced_ids),
            diagnostics=_external_reference_diagnostics(bundle),
        )


def session_archive_index(manifest: ArchiveManifest) -> ArchiveIndex:
    """Project only selectable Session headers, without decoding runtime histories."""
    try:
        items = []
        for item in manifest.data["bundle"]["sessions"]:
            session = item["snapshot"]["session"]
            if session["session_kind"] == "primary":
                items.append(
                    ArchiveSelectionItem(
                        identity=session["session_id"], label=session["title"] or "Untitled session"
                    )
                )
    except (KeyError, TypeError, ValidationError) as exc:
        raise DataTransferError("invalid Session selection data") from exc
    return ArchiveIndex(format="ouro-data-index", version=1, kind="sessions", items=tuple(items))


def _select_roots(bundle: SessionBundle, roots: set[str]) -> SessionBundle:
    return bundle.model_copy(
        update={
            "sessions": [
                item
                for item in bundle.sessions
                if item.snapshot.session.agent_root_session_id in roots
            ],
            "delegations": [item for item in bundle.delegations if item.root_session_id in roots],
        }
    )


def _skip_existing_trees(bundle: SessionBundle, existing: set[str]) -> SessionBundle:
    roots = {item.snapshot.session.agent_root_session_id for item in bundle.sessions}
    collisions = {
        item.snapshot.session.agent_root_session_id
        for item in bundle.sessions
        if item.snapshot.session.session_id in existing
    }
    return _select_roots(bundle, roots - collisions)


def _external_reference_diagnostics(bundle: SessionBundle) -> tuple[Diagnostic, ...]:
    """Describe preserved references without loading foreign plugin code.

    Availability is deliberately not inferred from names or installation alone:
    the normal Runtime checks exact extension and tool contracts on continuation.
    Historical records remain inspectable even when that check cannot pass.
    """
    diagnostics: list[Diagnostic] = []
    for item in bundle.sessions:
        snapshot = item.snapshot
        plugins = {
            state.owner_id
            for version in snapshot.harness_versions
            for state in version.state.states
        }
        plugins.update(
            activation.plugin_id
            for version in snapshot.harness_versions
            for activation in version.state.controls.activations
        )
        tools = {call.name for message in snapshot.messages for call in message.tool_calls}
        tools.update(
            message.tool_result.tool_name
            for message in snapshot.messages
            if message.tool_result is not None
        )
        model = snapshot.session.current_model
        paths = sorted(
            {
                file.path
                for message in snapshot.messages
                for file in message.input_attachments
                if isinstance(file, InputFileReference)
            }
        )
        if paths:
            diagnostics.append(
                Diagnostic(
                    code="session_file_references_preserved",
                    severity="info",
                    component="data_transfer",
                    message=(
                        "Original file references require the source workspace; "
                        "files were not copied."
                    ),
                    session_id=snapshot.session.session_id,
                    metadata={"paths": paths},
                )
            )
        if plugins or tools or model:
            diagnostics.append(
                Diagnostic(
                    code="session_external_references_preserved",
                    severity="info",
                    component="data_transfer",
                    message=(
                        "External references were preserved; "
                        "target compatibility is checked when continuing."
                    ),
                    session_id=snapshot.session.session_id,
                    metadata={"plugins": sorted(plugins), "tools": sorted(tools), "model": model},
                )
            )
    return tuple(diagnostics)


def _resource_files(bundle: SessionBundle) -> dict[str, tuple[str, ResourceRecord]]:
    files: dict[str, tuple[str, ResourceRecord]] = {}
    seen: set[str] = set()
    for item in bundle.sessions:
        session_id = item.snapshot.session.session_id
        ids = {record.resource_id for record in item.resources}
        messages = list(item.snapshot.messages)
        if item.latest_context is not None:
            messages.extend(item.latest_context.source_messages)
        required = {identity for message in messages for identity in message.resource_refs}
        required.update(
            attachment.resource_id
            for message in messages
            for attachment in message.input_attachments
            if isinstance(attachment, InputAttachment)
        )
        if len(ids) != len(item.resources):
            raise DataTransferError("Session archive contains duplicate resource identities")
        for raw in item.snapshot.session.metadata.get(QUEUED_INPUTS_KEY, []):
            try:
                queued = QueuedInput.model_validate(raw)
            except ValidationError as exc:
                raise DataTransferError("Session archive has an invalid queued input") from exc
            required.update(queued.resource_refs)
        if not required.issubset(ids):
            raise DataTransferError("Session archive resource references are incomplete")
        for record in item.resources:
            name = f"sessions/{session_id}/resources/files/{record.filename}"
            if name.casefold() in seen:
                raise DataTransferError("Session archive resource filenames collide")
            seen.add(name.casefold())
            files[name] = (session_id, record)
    return files
