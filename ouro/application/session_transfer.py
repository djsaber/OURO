from __future__ import annotations

import hashlib
from contextlib import ExitStack

from pydantic import ConfigDict, Field, ValidationError

from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.core.base import OuroModel
from ouro.core.errors import SessionBusyError
from ouro.core.resources import ResourceRecord
from ouro.resources.base import BlobStore
from ouro.sessions.base import SessionStore
from ouro.sessions.portable import PortableSession, SessionBundle
from ouro.transfer.archive import (
    MAX_ARCHIVE_BYTES,
    ArchiveManifest,
    DataArchive,
    DataTransferError,
)


class SessionArchiveData(OuroModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    bundle: SessionBundle
    resources: list[ResourceRecord] = Field(default_factory=list)


class SessionImportResult(ApplicationDTO):
    count: int
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionTransferService:
    """Transfer inactive Session trees and their immutable resource closure."""

    def __init__(self, sessions: SessionStore, resources: BlobStore) -> None:
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
            resource_records: list[ResourceRecord] = []
            size = 0
            for identity in sorted(_resource_ids(bundle)):
                resource_record, content = self.resources.load_content(
                    identity, max_bytes=MAX_ARCHIVE_BYTES
                )
                size += len(content)
                if size > MAX_ARCHIVE_BYTES:
                    raise DataTransferError("Session resources exceed the archive size limit")
                resource_records.append(resource_record)
                files[f"resources/{identity}"] = content
        data = SessionArchiveData(bundle=bundle, resources=resource_records)
        return DataArchive(
            ArchiveManifest(kind="sessions", data=data.model_dump(mode="json")), files
        )

    def import_data(
        self,
        archive: DataArchive,
        identities: tuple[str, ...] = (),
    ) -> SessionImportResult:
        try:
            data = SessionArchiveData.model_validate(archive.manifest.data)
        except ValidationError as exc:
            raise DataTransferError("invalid or unsupported Session archive data") from exc
        resources = {record.resource_id: record for record in data.resources}
        expected = _resource_ids(data.bundle)
        if len(resources) != len(data.resources) or set(resources) != expected:
            raise DataTransferError("Session archive resource references are incomplete")
        if set(archive.files) != {f"resources/{identity}" for identity in resources}:
            raise DataTransferError("Session archive resource files do not match its manifest")
        for identity, record in resources.items():
            content = archive.files[f"resources/{identity}"]
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
            data.bundle = SessionBundle(
                layout_version=data.bundle.layout_version,
                sessions=[
                    item
                    for item in data.bundle.sessions
                    if item.snapshot.session.agent_root_session_id in roots
                ],
                delegations=[
                    item for item in data.bundle.delegations if item.root_session_id in roots
                ],
            )
            selected_resources = _resource_ids(data.bundle)
            resources = {
                key: value for key, value in resources.items() if key in selected_resources
            }
        existing = {record.session_id for record in self.sessions.list_sessions()}
        if any(item.snapshot.session.session_id in existing for item in data.bundle.sessions):
            raise DataTransferError("Session already exists; import into a different home")
        # Blobs are immutable and may be shared. Publish them before references;
        # a failed Session commit can leave unreachable blobs, never broken refs.
        for identity, record in resources.items():
            self.resources.import_content(record, archive.files[f"resources/{identity}"])
        self.sessions.import_bundle(data.bundle)
        return SessionImportResult(
            count=len(data.bundle.sessions),
            diagnostics=_external_reference_diagnostics(data.bundle),
        )


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


def _resource_ids(bundle: SessionBundle) -> set[str]:
    identities: set[str] = set()
    for item in bundle.sessions:
        identities.update(reference.resource_id for reference in item.resources)
        for message in item.snapshot.messages:
            identities.update(message.resource_refs)
        for commit in item.snapshot.commits:
            for message in commit.messages:
                identities.update(message.resource_refs)
        for run in item.snapshot.runs:
            if run.active_tool_batch is not None:
                for call in run.active_tool_batch.calls:
                    if call.result is not None:
                        identities.update(call.result.resource_refs)
        if item.latest_context is not None:
            for message in item.latest_context.source_messages:
                identities.update(message.resource_refs)
    for delegation in bundle.delegations:
        identities.update(delegation.task_resource_refs)
        if delegation.result is not None:
            identities.update(delegation.result.resource_refs)
    return identities
