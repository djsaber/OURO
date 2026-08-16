from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ouro.application.events import RuntimeEventRecorder
from ouro.application.resources import ResourceService
from ouro.application.runs import RunLifecycle
from ouro.application.sessions import SessionService
from ouro.core.errors import (
    MigrationRequiredError,
    PersistenceError,
    ResourceAttachmentError,
    ResourceIntegrityError,
    ResourceInUseError,
    ResourceNotFoundError,
    SessionBusyError,
    UnsafeResourcePathError,
    UnsupportedMediaTypeError,
)
from ouro.core.messages import ImageContent, MessageRecord
from ouro.core.observers import ObserverRegistry
from ouro.core.state import RunRecord
from ouro.core.tools import ToolResult
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore


def _services(tmp_path, *, max_bytes=1024 * 1024):
    sessions = LocalSessionStore(tmp_path)
    sessions.initialize()
    resource_store = LocalResourceStore(sessions.root)
    resource_store.initialize()
    resources = ResourceService(
        store=resource_store,
        sessions=sessions,
        max_resource_bytes=max_bytes,
    )
    return sessions, resource_store, resources, SessionService(sessions, resources=resources)


def test_import_is_content_addressed_deduplicated_and_does_not_expose_source_path(
    tmp_path,
) -> None:
    sessions, resource_store, resources, session_service = _services(tmp_path)
    first_session = session_service.create(title="First").session
    second_session = session_service.create(title="Second").session
    source = tmp_path / "outside" / "notes.txt"
    source.parent.mkdir()
    source.write_text("same bytes", encoding="utf-8")

    first = resources.import_resource(first_session.session_id, source)
    repeated = resources.import_resource(first_session.session_id, source)
    shared = resources.import_resource(second_session.session_id, source)

    assert first.created is True
    assert repeated.created is False
    assert shared.resource.resource_id == first.resource.resource_id
    assert len(list(resource_store.blobs_dir.iterdir())) == 1
    assert resources.session_resource_ids(first_session.session_id) == (first.resource.resource_id,)
    assert str(source.parent) not in json.dumps(first.model_dump(mode="json"))
    catalog = {entry.session_id: entry for entry in session_service.catalog().entries}
    assert catalog[first_session.session_id].resource_count == 1
    assert catalog[second_session.session_id].resource_count == 1


def test_resource_store_rejects_record_without_explicit_schema_version(tmp_path) -> None:
    _, resource_store, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Versioned resource").session
    source = tmp_path / "versioned.txt"
    source.write_text("versioned", encoding="utf-8")
    imported = resources.import_resource(session.session_id, source)
    record_path = resource_store.records_dir / f"{imported.resource.resource_id}.json"
    raw = json.loads(record_path.read_text("utf-8"))
    raw.pop("schema_version")
    record_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(MigrationRequiredError, match="found missing, expected 1"):
        resource_store.load_record(imported.resource.resource_id)


def test_failed_oversized_import_leaves_no_visible_reference_or_staging_file(tmp_path) -> None:
    sessions, resource_store, resources, session_service = _services(tmp_path, max_bytes=4)
    session = session_service.create(title="Limits").session
    source = tmp_path / "large.bin"
    source.write_bytes(b"12345")

    with pytest.raises(ValueError, match="maximum size"):
        resources.import_resource(session.session_id, source)

    assert resources.list_resources(session.session_id).resources == ()
    assert list(resource_store.staging_dir.iterdir()) == []
    assert list(resource_store.records_dir.iterdir()) == []
    assert sessions.load_session(session.session_id).messages == []


def test_reference_publish_failure_rolls_back_new_canonical_record(
    monkeypatch,
    tmp_path,
) -> None:
    sessions, resource_store, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Rollback").session
    source = tmp_path / "rollback.txt"
    source.write_text("rollback", encoding="utf-8")

    def fail_attach(_session_id, _reference):
        raise OSError("injected reference failure")

    monkeypatch.setattr(sessions, "attach_resource_reference", fail_attach)

    with pytest.raises(OSError, match="injected reference failure"):
        resources.import_resource(session.session_id, source)

    assert list(resource_store.records_dir.iterdir()) == []
    assert list(resource_store.blobs_dir.iterdir()) == []
    assert resources.list_resources(session.session_id).resources == ()


def test_index_failure_keeps_canonical_import_and_returns_diagnostic(
    monkeypatch,
    tmp_path,
) -> None:
    _, resource_store, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Index").session
    source = tmp_path / "index.txt"
    source.write_text("canonical", encoding="utf-8")
    original_scan = resource_store._scan_records

    def fail_scan():
        raise PersistenceError("injected resource index failure")

    monkeypatch.setattr(resource_store, "_scan_records", fail_scan)

    imported = resources.import_resource(session.session_id, source)

    assert imported.diagnostics[0].code == "resource_index_refresh_failed"
    assert resources.show_resource(session.session_id, imported.resource.resource_id).size == 9
    assert len(list(resource_store.records_dir.iterdir())) == 1
    assert resource_store.index_dirty_path.is_file()

    monkeypatch.setattr(resource_store, "_scan_records", original_scan)
    resource_store.rebuild_index()
    assert not resource_store.index_dirty_path.exists()


def test_invalid_media_type_and_non_file_source_use_typed_errors(tmp_path) -> None:
    _, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Invalid input").session
    source = tmp_path / "media.txt"
    source.write_text("media", encoding="utf-8")

    with pytest.raises(UnsupportedMediaTypeError, match="media_type"):
        resources.import_resource(
            session.session_id,
            source,
            media_type="not-a-media-type",
        )

    with pytest.raises(UnsafeResourcePathError, match="not a regular file"):
        resources.import_resource(session.session_id, tmp_path)


def test_concurrent_imports_to_distinct_sessions_share_one_blob(tmp_path) -> None:
    _, resource_store, resources, session_service = _services(tmp_path)
    first = session_service.create(title="First").session
    second = session_service.create(title="Second").session
    source = tmp_path / "concurrent.bin"
    source.write_bytes(b"concurrent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(resources.import_resource, session_id, source)
            for session_id in (first.session_id, second.session_id)
        ]
        results = [future.result() for future in futures]

    assert results[0].resource.resource_id == results[1].resource.resource_id
    assert len(list(resource_store.records_dir.iterdir())) == 1
    assert len(list(resource_store.blobs_dir.iterdir())) == 1


def test_resource_mutation_respects_session_lifecycle_lock(tmp_path) -> None:
    sessions, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Busy").session
    source = tmp_path / "busy.txt"
    source.write_text("busy", encoding="utf-8")
    lock = sessions.acquire_session_lock(session.session_id, "run_active")
    assert lock is not None
    try:
        with pytest.raises(SessionBusyError, match="active run"):
            resources.import_resource(session.session_id, source)
    finally:
        lock.release()

    assert resources.list_resources(session.session_id).resources == ()


def test_integrity_mismatch_is_detected_before_show_or_export(tmp_path) -> None:
    _, resource_store, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Integrity").session
    source = tmp_path / "data.bin"
    source.write_bytes(b"canonical")
    imported = resources.import_resource(session.session_id, source)
    record = resource_store.load_record(imported.resource.resource_id)
    resource_store._blob_path(record).write_bytes(b"corrupt")

    with pytest.raises(ResourceIntegrityError, match="integrity mismatch"):
        resources.show_resource(session.session_id, record.resource_id)

    with pytest.raises(ResourceIntegrityError):
        resources.export_resource(session.session_id, record.resource_id, tmp_path / "copy.bin")


def test_branch_atomically_copies_valid_resource_relationships(tmp_path) -> None:
    sessions, resource_store, resources, session_service = _services(tmp_path)
    source_session = session_service.create(title="Source").session
    source_file = tmp_path / "attachment.txt"
    source_file.write_text("attachment", encoding="utf-8")
    resource = resources.import_resource(source_session.session_id, source_file).resource
    message = MessageRecord.text(
        session_id=source_session.session_id,
        role="user",
        text="use attachment",
        sequence=1,
    ).model_copy(update={"resource_refs": [resource.resource_id]})
    sessions.append_message(message)

    branch = session_service.branch(source_session.session_id, message.message_id)
    branch_snapshot = sessions.load_session(branch.session.session_id)

    assert branch_snapshot.messages[0].resource_refs == [resource.resource_id]
    assert resources.session_resource_ids(branch.session.session_id) == (resource.resource_id,)
    reference = sessions.load_resource_references(branch.session.session_id)[0]
    assert reference.copied_from_session_id == source_session.session_id


def test_branch_publish_precedes_concurrent_source_trash(monkeypatch, tmp_path) -> None:
    sessions, resource_store, resources, session_service = _services(tmp_path)
    source_session = session_service.create(title="Branch and trash").session
    source_file = tmp_path / "branch-trash.txt"
    source_file.write_text("shared", encoding="utf-8")
    resource = resources.import_resource(source_session.session_id, source_file).resource
    message = MessageRecord.text(
        session_id=source_session.session_id,
        role="user",
        text="keep attachment",
        sequence=1,
    ).model_copy(update={"resource_refs": [resource.resource_id]})
    sessions.append_message(message)
    branch_validating = Event()
    allow_branch = Event()
    original_branch_references = resources.branch_references

    def blocked_branch_references(source_session_id, target_session_id, resource_ids):
        branch_validating.set()
        assert allow_branch.wait(timeout=2)
        return original_branch_references(source_session_id, target_session_id, resource_ids)

    monkeypatch.setattr(resources, "branch_references", blocked_branch_references)
    with ThreadPoolExecutor(max_workers=2) as executor:
        branch_future = executor.submit(
            session_service.branch,
            source_session.session_id,
            message.message_id,
        )
        assert branch_validating.wait(timeout=2)
        trash_future = executor.submit(session_service.trash, source_session.session_id)
        assert not trash_future.done()
        allow_branch.set()
        branch = branch_future.result(timeout=2)
        trash_future.result(timeout=2)

    assert resources.session_resource_ids(branch.session.session_id) == (resource.resource_id,)
    assert resource_store.load_record(resource.resource_id).resource_id == resource.resource_id


def test_remove_rejects_canonical_message_reference(tmp_path) -> None:
    sessions, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Used").session
    source = tmp_path / "used.txt"
    source.write_text("used", encoding="utf-8")
    imported = resources.import_resource(session.session_id, source).resource
    message = MessageRecord.text(
        session_id=session.session_id,
        role="user",
        text="attached",
        sequence=1,
    ).model_copy(update={"resource_refs": [imported.resource_id]})
    sessions.append_message(message)

    with pytest.raises(ResourceInUseError, match=message.message_id):
        resources.remove_resource(session.session_id, imported.resource_id)

    assert resources.session_resource_ids(session.session_id) == (imported.resource_id,)


def test_remove_collects_content_only_after_last_session_reference(tmp_path) -> None:
    _, resource_store, resources, session_service = _services(tmp_path)
    first = session_service.create(title="First").session
    second = session_service.create(title="Second").session
    source = tmp_path / "shared.txt"
    source.write_text("shared", encoding="utf-8")
    imported = resources.import_resource(first.session_id, source).resource
    resources.import_resource(second.session_id, source)

    first_removed = resources.remove_resource(first.session_id, imported.resource_id)
    assert first_removed.content_collected is False
    assert resource_store.load_record(imported.resource_id).resource_id == imported.resource_id

    second_removed = resources.remove_resource(second.session_id, imported.resource_id)
    assert second_removed.content_collected is True
    assert list(resource_store.records_dir.iterdir()) == []
    assert list(resource_store.blobs_dir.iterdir()) == []


def test_trashing_session_collects_unreachable_content_after_canonical_publish(tmp_path) -> None:
    _, resource_store, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Trash").session
    source = tmp_path / "trash.txt"
    source.write_text("trash", encoding="utf-8")
    resource_id = resources.import_resource(session.session_id, source).resource.resource_id

    result = session_service.trash(session.session_id)

    assert (resource_store.root / "trash" / "sessions" / result.trash_id).is_dir()
    assert list(resource_store.records_dir.iterdir()) == []
    assert list(resource_store.blobs_dir.iterdir()) == []
    assert resource_id not in resource_store.index_path.read_text("utf-8")


def test_symbolic_link_import_is_rejected_when_platform_supports_links(tmp_path) -> None:
    _, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Links").session
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(UnsafeResourcePathError, match="symbolic links"):
        resources.import_resource(session.session_id, link)


def test_tool_resource_refs_are_validated_before_canonical_message_commit(tmp_path) -> None:
    sessions, resource_store, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Tool refs").session
    recorder = RuntimeEventRecorder(store=sessions, observers=ObserverRegistry())
    lifecycle = RunLifecycle(
        store=sessions,
        events=recorder,
        validate_resource_refs=resources.validate_references,
    )
    run = RunRecord(session_id=session.session_id, status="running")

    with pytest.raises(ResourceNotFoundError, match="not attached"):
        lifecycle.append_tool_result_message(
            session=sessions.load_session_record(session.session_id),
            run=run,
            result=ToolResult(
                tool_call_id="call_1",
                tool_name="GeneratedFile",
                resource_refs=["res_" + "0" * 64],
            ),
            sequence=1,
        )

    assert sessions.load_session(session.session_id).messages == []
    assert sessions.load_resource_references(session.session_id) == []


def test_text_resource_is_projected_with_source_label_without_mutating_canonical_message(
    tmp_path,
) -> None:
    _, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Projection").session
    source = tmp_path / "notes.txt"
    source.write_text("important context", encoding="utf-8")
    resource = resources.import_resource(session.session_id, source).resource
    canonical = MessageRecord.text(
        session_id=session.session_id,
        role="user",
        text="Summarize this",
    ).model_copy(update={"resource_refs": [resource.resource_id]})

    projected = resources.project_model_messages([canonical], supports_vision=False)[0]

    assert canonical.text_content == "Summarize this"
    assert canonical.resource_refs == [resource.resource_id]
    assert "Attached resource: notes.txt" in projected.text_content
    assert resource.resource_id in projected.text_content
    assert projected.text_content.endswith("important context")


def test_image_resource_projection_is_request_local_data_uri(tmp_path) -> None:
    _, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Image").session
    source = tmp_path / "pixel.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")
    resource = resources.import_resource(
        session.session_id,
        source,
        media_type="image/png",
    ).resource
    canonical = MessageRecord.text(
        session_id=session.session_id,
        role="user",
        text="Describe it",
    ).model_copy(update={"resource_refs": [resource.resource_id]})

    projected = resources.project_model_messages([canonical], supports_vision=True)[0]
    image = next(block for block in projected.content if isinstance(block, ImageContent))

    assert image.uri.startswith("data:image/png;base64,")
    assert all(not isinstance(block, ImageContent) for block in canonical.content)


def test_model_attachment_rejects_unsupported_media_and_non_vision_endpoint(tmp_path) -> None:
    _, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Unsupported").session
    binary = tmp_path / "archive.bin"
    binary.write_bytes(b"binary")
    binary_resource = resources.import_resource(
        session.session_id,
        binary,
        media_type="application/octet-stream",
    ).resource

    with pytest.raises(UnsupportedMediaTypeError, match="not supported for model input"):
        resources.validate_model_references(
            session.session_id,
            [binary_resource.resource_id],
            supports_vision=False,
        )

    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    image_resource = resources.import_resource(
        session.session_id,
        image,
        media_type="image/png",
    ).resource
    with pytest.raises(ResourceAttachmentError, match="does not support image attachment"):
        resources.validate_model_references(
            session.session_id,
            [image_resource.resource_id],
            supports_vision=False,
        )
