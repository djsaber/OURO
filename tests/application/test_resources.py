from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import ouro.application.inputs.files as file_inputs
from ouro.application.events import RuntimeEventRecorder
from ouro.application.resources import ResourceService
from ouro.application.runs.lifecycle import RunLifecycle
from ouro.application.sessions.service import SessionService
from ouro.core.errors import (
    MediaCapacityError,
    PersistenceError,
    ResourceIntegrityError,
    ResourceInUseError,
    ResourceNotFoundError,
    SessionBusyError,
    UnsafeResourcePathError,
    UnsupportedMediaTypeError,
)
from ouro.core.messages import ImageContent, MessageRecord, ModelMessage, ToolResultReference
from ouro.core.observers import ObserverRegistry
from ouro.core.state import RunRecord
from ouro.core.tools import ToolResult
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore
from tests.providers.test_tool_result_content import encode


def test_file_batch_failure_keeps_only_verified_published_files(tmp_path):
    from tests.resource_fixtures import resource_record

    store = LocalResourceStore(tmp_path)
    first = resource_record(content=b"first", filename="first.txt")
    second = resource_record(content=b"second", filename="second.txt")
    with pytest.raises(ResourceIntegrityError):
        store.import_contents("ses_test", ((first, b"first"), (second, b"bad")))
    assert store.load_content("ses_test", first) == b"first"
    assert not store.path_for("ses_test", second).exists()
    assert list(store.directory_for("ses_test").iterdir()) == [store.path_for("ses_test", first)]


def test_file_batch_checks_existing_and_incoming_content(tmp_path):
    from tests.resource_fixtures import resource_record

    store = LocalResourceStore(tmp_path)
    record = resource_record(content=b"verified")
    store.import_contents("ses_test", ((record, b"verified"),))
    with pytest.raises(ResourceIntegrityError):
        store.import_contents("ses_test", ((record, b"modified"),))
    assert store.load_content("ses_test", record) == b"verified"
    store.path_for("ses_test", record).write_bytes(b"corrupt")
    with pytest.raises(ResourceIntegrityError):
        store.import_contents("ses_test", ((record, b"verified"),))


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


@pytest.mark.parametrize("image", (False, True))
def test_model_projection_reads_file_once_and_revalidates_each_request(
    tmp_path, monkeypatch, image
):
    _, store, resources, sessions = _services(tmp_path)
    session_id = sessions.create().session.session_id
    source = tmp_path / ("image.png" if image else "input.txt")
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes" if image else b"attached text")
    resource = resources.import_resource(session_id, source).resource
    message = MessageRecord.text(session_id=session_id, role="user", text="Read this")
    message.resource_refs = [resource.resource_id]
    reads = []
    open_path = type(source).open
    read_bytes = type(source).read_bytes
    blob_path = resources.resource_path(session_id, resource.resource_id)

    def tracked_open(path, *args, **kwargs):
        if path == blob_path and args and args[0] == "rb":
            reads.append(path)
        return open_path(path, *args, **kwargs)

    def tracked_read(path):
        if path == blob_path:
            reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(type(source), "open", tracked_open)
    monkeypatch.setattr(type(source), "read_bytes", tracked_read)
    projected = resources.project_model_messages(
        [message], input_modalities=frozenset({"image"}) if image else frozenset()
    )
    if image:
        assert any(isinstance(block, ImageContent) for block in projected[0].content)
    else:
        assert "attached text" in projected[0].text_content
    assert message.text_content == "Read this"
    assert len(reads) == 1
    # Integrity is checked against the bytes consumed by every request.
    blob_path.write_bytes(b"corrupt")
    with pytest.raises(ResourceIntegrityError):
        resources.project_model_messages([message], input_modalities=frozenset())


def test_projection_loads_session_manifest_once_per_request(tmp_path, monkeypatch):
    store, _, resources, service = _services(tmp_path)
    session_id = service.create().session.session_id
    source = tmp_path / "input.txt"
    source.write_text("data", encoding="utf-8")
    records = [resources.import_resource(session_id, source).resource for _ in range(3)]
    message = MessageRecord.text(session_id=session_id, role="user", text="Read these")
    message.resource_refs = [record.resource_id for record in records]
    reads = []
    load = store.load_resource_references

    def tracked_load(identity):
        reads.append(identity)
        return load(identity)

    monkeypatch.setattr(store, "load_resource_references", tracked_load)
    for _ in range(2):
        resources.project_model_messages([message], input_modalities=frozenset())
    assert reads == [session_id, session_id]


def test_failed_file_removal_preserves_resource_ownership(tmp_path, monkeypatch):
    sessions, store, resources, service = _services(tmp_path)
    session_id = service.create().session.session_id
    source = tmp_path / "locked.txt"
    source.write_text("data", encoding="utf-8")
    record = resources.import_resource(session_id, source).resource
    original = sessions.load_resource_references(session_id)

    def deny_removal(_session_id, _records):
        raise PermissionError("File is locked")

    monkeypatch.setattr(store, "delete_resources", deny_removal)
    with pytest.raises(PermissionError, match="File is locked"):
        resources.remove_resource(session_id, record.resource_id)
    assert sessions.load_resource_references(session_id) == original
    assert resources.resource_path(session_id, record.resource_id).read_bytes() == b"data"


def test_import_keeps_independent_named_copies_without_source_path(
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
    assert repeated.created is True
    assert (
        len(
            {shared.resource.resource_id, first.resource.resource_id, repeated.resource.resource_id}
        )
        == 3
    )
    assert [first.resource.filename, repeated.resource.filename] == ["notes.txt", "notes (2).txt"]
    assert len(resources.session_resource_ids(first_session.session_id)) == 2
    assert str(source.parent) not in json.dumps(first.model_dump(mode="json"))
    catalog = {entry.session_id: entry for entry in session_service.catalog().entries}
    assert catalog[first_session.session_id].resource_count == 2
    assert catalog[second_session.session_id].resource_count == 1


def test_session_resource_metadata_rejects_unsupported_version(tmp_path):
    sessions, _, resources, service = _services(tmp_path)
    session = service.create().session
    source = tmp_path / "versioned.txt"
    source.write_text("versioned", encoding="utf-8")
    resources.import_resource(session.session_id, source)
    record_path = sessions.sessions_dir / session.session_id / "resources" / "index.json"
    raw = json.loads(record_path.read_text("utf-8"))
    raw["resources"][0]["schema_version"] = 1
    record_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PersistenceError):
        resources.list_resources(session.session_id)


def test_failed_oversized_import_leaves_no_visible_reference_or_staging_file(tmp_path) -> None:
    sessions, resource_store, resources, session_service = _services(tmp_path, max_bytes=4)
    session = session_service.create(title="Limits").session
    source = tmp_path / "large.bin"
    source.write_bytes(b"12345")

    with pytest.raises(ValueError, match="maximum size"):
        resources.import_resource(session.session_id, source)

    assert resources.list_resources(session.session_id).resources == ()
    assert (
        list((resource_store.directory_for(session.session_id).parent / ".staging").iterdir()) == []
    )
    assert list(resource_store.directory_for(session.session_id).iterdir()) == []
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

    assert list(resource_store.directory_for(session.session_id).iterdir()) == []
    assert list(resource_store.directory_for(session.session_id).iterdir()) == []
    assert resources.list_resources(session.session_id).resources == ()


def test_index_failure_keeps_canonical_import_and_returns_diagnostic(monkeypatch, tmp_path):
    sessions, _, resources, service = _services(tmp_path)
    session = service.create().session
    source = tmp_path / "index.txt"
    source.write_text("canonical", encoding="utf-8")
    monkeypatch.setattr(sessions, "refresh_catalog", lambda _sid: "injected index failure")
    imported = resources.import_resource(session.session_id, source)
    assert imported.diagnostics[0].code == "session_index_update_failed"
    assert resources.show_resource(session.session_id, imported.resource.resource_id).size == 9
    assert (
        resources.resource_path(session.session_id, imported.resource.resource_id).read_bytes()
        == b"canonical"
    )


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


def test_concurrent_imports_allocate_independent_session_copies(tmp_path) -> None:
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

    assert results[0].resource.resource_id != results[1].resource.resource_id
    for session, result in zip((first, second), results, strict=True):
        assert (
            resources.resource_path(session.session_id, result.resource.resource_id).read_bytes()
            == b"concurrent"
        )


def test_import_allows_preparing_follow_up_while_removal_respects_run_lock(tmp_path) -> None:
    sessions, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Busy").session
    source = tmp_path / "busy.txt"
    source.write_text("busy", encoding="utf-8")
    lock = sessions.acquire_session_lock(session.session_id, "run_active")
    assert lock is not None
    try:
        resource = resources.import_resource(session.session_id, source).resource
        with pytest.raises(SessionBusyError, match="active run"):
            resources.remove_resource(session.session_id, resource.resource_id)
    finally:
        lock.release()

    assert len(resources.list_resources(session.session_id).resources) == 1


def test_integrity_mismatch_is_detected_before_show_or_export(tmp_path) -> None:
    _, resource_store, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Integrity").session
    source = tmp_path / "data.bin"
    source.write_bytes(b"canonical")
    imported = resources.import_resource(session.session_id, source)
    record = imported.resource
    resources.resource_path(session.session_id, record.resource_id).write_bytes(b"corrupt")

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

    def blocked_branch_references(source_session_id, resource_ids):
        branch_validating.set()
        assert allow_branch.wait(timeout=2)
        return original_branch_references(source_session_id, resource_ids)

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
    assert (
        resources.resource_path(branch.session.session_id, resource.resource_id).read_bytes()
        == source_file.read_bytes()
    )


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

    with pytest.raises(ResourceInUseError, match="canonical message"):
        resources.remove_resource(session.session_id, imported.resource_id)

    assert resources.session_resource_ids(session.session_id) == (imported.resource_id,)


def test_remove_collects_only_the_owning_sessions_copy(tmp_path):
    _, _, resources, service = _services(tmp_path)
    first, second = service.create().session, service.create().session
    source = tmp_path / "shared.txt"
    source.write_bytes(b"shared")
    one = resources.import_resource(first.session_id, source).resource
    two = resources.import_resource(second.session_id, source).resource
    first_path = resources.resource_path(first.session_id, one.resource_id)
    assert resources.remove_resource(first.session_id, one.resource_id).content_collected
    assert not first_path.exists()
    assert resources.resource_path(second.session_id, two.resource_id).read_bytes() == b"shared"
    assert resources.remove_resource(second.session_id, two.resource_id).content_collected


@pytest.mark.parametrize("scope_source", ("canonical", "catalog"))
def test_trashing_session_retains_owned_files_with_its_metadata(
    tmp_path,
    scope_source: str,
) -> None:
    _, resource_store, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Trash").session
    source = tmp_path / "trash.txt"
    source.write_text("trash", encoding="utf-8")
    resources.import_resource(session.session_id, source)

    result = (
        session_service.trash(session.session_id)
        if scope_source == "canonical"
        else session_service.trash_from_catalog(session.session_id)
    )

    trash = resource_store.root / "trash" / "sessions" / result.trash_id
    assert trash.is_dir()
    assert not resource_store.directory_for(session.session_id).exists()
    assert next(trash.rglob("trash.txt")).read_bytes() == b"trash"


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


def test_tool_result_batch_validates_every_resource_before_any_message_commit(tmp_path) -> None:
    sessions, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Tool batch refs").session
    source = tmp_path / "valid.txt"
    source.write_text("valid", encoding="utf-8")
    valid_resource = resources.import_resource(session.session_id, source).resource
    recorder = RuntimeEventRecorder(store=sessions, observers=ObserverRegistry())
    lifecycle = RunLifecycle(
        store=sessions,
        events=recorder,
        validate_resource_refs=resources.validate_references,
    )
    run = RunRecord(session_id=session.session_id, status="running")

    with pytest.raises(ResourceNotFoundError, match="not attached"):
        lifecycle.append_tool_result_messages(
            session=sessions.load_session_record(session.session_id),
            run=run,
            results=[
                ToolResult(
                    tool_call_id="call_valid",
                    tool_name="GeneratedFile",
                    resource_refs=[valid_resource.resource_id],
                ),
                ToolResult(
                    tool_call_id="call_invalid",
                    tool_name="GeneratedFile",
                    resource_refs=["res_" + "0" * 64],
                ),
            ],
            first_sequence=1,
        )

    assert sessions.load_session(session.session_id).messages == []


def test_tool_result_batch_reuses_existing_message_and_commits_only_missing_suffix(
    tmp_path,
) -> None:
    sessions, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="Tool batch recovery").session
    recorder = RuntimeEventRecorder(store=sessions, observers=ObserverRegistry())
    lifecycle = RunLifecycle(
        store=sessions,
        events=recorder,
        validate_resource_refs=resources.validate_references,
    )
    run = RunRecord(session_id=session.session_id, status="running")
    sessions.save_run(run)
    first = ToolResult(tool_call_id="call_1", tool_name="First", content="one")
    second = ToolResult(tool_call_id="call_2", tool_name="Second", content="two")
    existing = lifecycle.append_tool_result_message(
        session=sessions.load_session_record(session.session_id),
        run=run,
        result=first,
        sequence=1,
    )

    messages = lifecycle.append_tool_result_messages(
        session=sessions.load_session_record(session.session_id),
        run=run,
        results=[first, second],
        first_sequence=1,
    )

    snapshot = sessions.load_session(session.session_id)
    assert messages[0] == existing
    assert [message.sequence for message in messages] == [1, 2]
    assert [message.tool_result.tool_call_id for message in snapshot.messages] == [
        "call_1",
        "call_2",
    ]


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

    projected = resources.project_model_messages([canonical], input_modalities=frozenset())[0]

    assert canonical.text_content == "Summarize this"
    assert canonical.resource_refs == [resource.resource_id]
    assert 'File: "notes.txt"' in projected.text_content
    assert resource.resource_id not in projected.text_content
    assert "important context" in projected.text_content
    assert (
        resources.resource_path(session.session_id, resource.resource_id).as_posix()
        in projected.text_content
    )


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

    projected = resources.project_model_messages(
        [canonical], input_modalities=frozenset({"image"})
    )[0]
    image = next(block for block in projected.content if isinstance(block, ImageContent))

    assert image.uri.startswith("data:image/png;base64,")
    assert all(not isinstance(block, ImageContent) for block in canonical.content)


def test_repeated_media_resource_preserves_each_occurrence_and_elides_without_encoding(
    tmp_path,
    monkeypatch,
):
    _, store, resources, service = _services(tmp_path)
    session_id = service.create().session.session_id
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16000)
    resource = resources.import_resource(session_id, path).resource
    old = MessageRecord.text(session_id=session_id, role="user", text="old image")
    old.resource_refs = [resource.resource_id]
    latest = old.model_copy(update={"message_id": "msg_latest"})
    before = resources.project_model_messages([old], input_modalities=frozenset({"image"}))
    both = resources.project_model_messages([old, latest], input_modalities=frozenset({"image"}))
    assert both[0] == before[0]
    assert any(isinstance(part, ImageContent) for part in both[1].content)
    encoded = []
    encode_image = base64.b64encode

    def record_encode(raw):
        encoded.append(len(raw))
        return encode_image(raw)

    monkeypatch.setattr(file_inputs.base64, "b64encode", record_encode)
    prepared = resources.prepare_model_messages(
        [old, latest], input_modalities=frozenset({"image"})
    )
    assert encoded == []
    result = prepared.materialize(frozenset({old.message_id}))
    assert encoded == [path.stat().st_size]
    assert "[Historical image]" in result[0].text_content
    assert "old image" in result[0].text_content
    assert result[1] == both[1]


@pytest.mark.parametrize("source_kind", ["inline", "resource"])
@pytest.mark.parametrize(
    "role,image_input,keeps_image",
    [
        ("tool_result", False, False),
        ("tool_result", True, True),
        ("user", False, False),
        ("user", True, True),
        ("assistant", False, False),
        ("assistant", True, False),
    ],
)
def test_resource_preparation_preserves_eligible_tool_content_for_provider_placement(
    tmp_path, source_kind, role, image_input, keeps_image
):
    _, _, resources, service = _services(tmp_path)
    session_id = service.create().session.session_id
    raw = b"\x89PNG\r\n\x1a\nimage-bytes"
    uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    message = MessageRecord.text(session_id=session_id, role=role, text="image output")
    if role == "tool_result":
        message.tool_result = ToolResultReference(tool_call_id="call", tool_name="Render")
    if source_kind == "inline":
        message.content.append(ImageContent(uri=uri, media_type="image/png"))
    else:
        path = tmp_path / "image.png"
        path.write_bytes(raw)
        message.resource_refs = [resources.import_resource(session_id, path).resource.resource_id]
    before = message.model_copy(deep=True)

    projected = resources.project_model_messages(
        [message], input_modalities=frozenset({"image"}) if image_input else frozenset()
    )[0]

    images = [part for part in projected.content if isinstance(part, ImageContent)]
    assert [part.uri for part in images] == ([uri] if keeps_image else [])
    assert message == before
    if role == "tool_result":
        assert ("not sent" in projected.text_content) == (not image_input)
        for protocol in ("responses", "messages", "chat"):
            wire = str(
                encode(protocol, [ModelMessage.from_record(projected)], image_input=image_input)
            )
            assert (uri.partition(",")[2] in wire) == image_input
            if not image_input:
                assert wire.count("This model cannot view images.") == 1


@pytest.mark.parametrize("image_input", [False, True])
@pytest.mark.parametrize("earlier_role", ["user", "tool_result"])
def test_request_image_budget_is_shared_before_resource_encoding(
    tmp_path, monkeypatch, image_input, earlier_role
):
    _, _, resources, service = _services(tmp_path)
    session_id = service.create().session.session_id
    raw = b"\x89PNG\r\n\x1a\nimage-bytes"
    encoded = base64.b64encode(raw).decode("ascii")
    image = ImageContent(uri="data:image/png;base64," + encoded)
    latest = MessageRecord(
        session_id=session_id,
        role="tool_result",
        content=[image],
        tool_result=ToolResultReference(tool_call_id="latest", tool_name="Render"),
    )
    for index in range(2):
        path = tmp_path / f"{index}.png"
        path.write_bytes(raw)
        latest.resource_refs.append(
            resources.import_resource(session_id, path).resource.resource_id
        )
    earlier = MessageRecord(
        session_id=session_id,
        role=earlier_role,
        content=[image],
        tool_result=(
            ToolResultReference(tool_call_id="earlier", tool_name="Render")
            if earlier_role == "tool_result"
            else None
        ),
    )
    before = [message.model_copy(deep=True) for message in (earlier, latest)]
    monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", len(encoded) * 2)
    encode_image = base64.b64encode
    encoded_sources = []

    def bounded_encode(data):
        encoded_sources.append(data)
        return encode_image(data)

    monkeypatch.setattr(file_inputs.base64, "b64encode", bounded_encode)
    if image_input:
        with pytest.raises(MediaCapacityError, match="request media limit"):
            resources.project_model_messages(
                [earlier, latest], input_modalities=frozenset({"image"})
            )
    else:
        projected = resources.project_model_messages(
            [earlier, latest], input_modalities=frozenset()
        )
        assert all("This model cannot view images." in item.text_content for item in projected)
    assert encoded_sources == []
    assert [earlier, latest] == before


def test_model_attachment_accepts_binary_and_non_vision_images_as_references(tmp_path):
    _, _, resources, session_service = _services(tmp_path)
    session = session_service.create(title="References").session
    for name, data, media in (
        ("archive.bin", b"\x00\xff", "application/octet-stream"),
        ("image.png", b"\x89PNG\r\n\x1a\nimage-bytes", "image/png"),
    ):
        path = tmp_path / name
        path.write_bytes(data)
        resource = resources.import_resource(session.session_id, path, media_type=media).resource
        assert resources.validate_model_references(session.session_id, [resource.resource_id]) == (
            resource.resource_id,
        )
        message = MessageRecord.text(session_id=session.session_id, role="user", text="")
        message.resource_refs = [resource.resource_id]
        projected = resources.project_model_messages([message], input_modalities=frozenset())[0]
        assert "Representation: reference only" in projected.text_content
        assert not any(isinstance(block, ImageContent) for block in projected.content)
