from __future__ import annotations

import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest

import ouro.application.inputs.files as file_inputs
from ouro.application.runtime.service import RuntimeService
from ouro.application.transfer.sessions import SessionTransferService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.content import MediaContent
from ouro.core.context import ContextBuilder
from ouro.core.context_selection import MessageBoundary
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.errors import ResourceIntegrityError, ResourceInUseError, ResourceNotFoundError
from ouro.core.input_content import InputAttachment, InputFileReference
from ouro.core.messages import MessageRecord, ModelResponseRecord
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommit
from ouro.local_locks import FileMutationLock
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry
from tests.application.test_resources import _services


@pytest.fixture(params=["files", "sqlite"])
def services(tmp_path, request):
    return _services(tmp_path / "home", backend=request.param, max_bytes=2 * 1024 * 1024)


def observe(services, tmp_path, source_kind, size):
    store, files, resources, sessions = services
    session = sessions.create().session
    path = tmp_path / "picture.jpg"
    raw = b"\xff\xd8\xff" + b"content" * (size // 7)
    path.write_bytes(raw)
    if source_kind == "upload":
        resource = resources.import_resource(session.session_id, path).resource
        item = InputAttachment(name=path.name, resource_id=resource.resource_id)
    else:
        item = InputFileReference(name=path.name, path=str(path))
    message = resources.prepare_input(
        MessageRecord(
            session_id=session.session_id,
            role="user",
            semantic_tag="attachments",
            input_attachments=(item,),
            resource_refs=[item.resource_id] if isinstance(item, InputAttachment) else [],
        ),
        input_modalities=frozenset({"image"}),
        workspace=str(tmp_path),
        workspace_only=True,
    )
    store.append_message(message)
    path.unlink()
    return session, message, raw


def publish(store, session, message, *, canonical=True):
    view = ContextBuilder().build(
        messages=[message], source_messages=[message] if canonical else [], tools=[]
    )
    header = ModelRequestHeader(
        session_id=session.session_id,
        run_id="run",
        turn_id="turn",
        model_round=1,
        provider_id="test",
        model_id="test",
        metrics=view.metrics,
    )
    body = RequestManifestRecord(context_id=header.context_id, manifest=view.manifest)
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            request_header=header,
            request_manifest=body,
        )
    )
    return view


@pytest.mark.parametrize("size", [4096, 1024 * 1024])
@pytest.mark.parametrize("source_kind", ["upload", "reference"])
def test_media_has_one_resource_owner_and_bounded_message_and_request_facts(
    services, tmp_path, source_kind, size, monkeypatch
):
    store, files, resources, _ = services
    session, message, raw = observe(services, tmp_path, source_kind, size)
    records = store.load_resource_references(session.session_id)
    assert len(records) == 1
    record = records[0]
    block = next(block for block in message.content if isinstance(block, MediaContent))
    assert block.resource_id == record.resource_id
    assert record.internal is (source_kind == "reference")
    assert len(resources.list_resources(session.session_id).resources) == (source_kind == "upload")
    assert files.load_content(session.session_id, record) == raw
    fact = store.load_message_fact(session.session_id, message.message_id)
    assert len(fact.model_dump_json()) < 5000
    assert next(part for part in fact.content if isinstance(part, MediaContent)).uri == ""
    if isinstance(store, LocalSessionStore):
        payload = (store.sessions_dir / session.session_id / "messages.jsonl").read_text(
            encoding="utf-8"
        )
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            payload = connection.execute("SELECT payload FROM messages").fetchone()[0]
    assert len(payload) < 5000 and base64.b64encode(raw).decode() not in payload

    def forbidden(*args, **kwargs):
        pytest.fail("selected media read scanned or verified unrelated resources")

    monkeypatch.setattr(store, "load_resource_references", forbidden)
    monkeypatch.setattr(files, "verify", forbidden)
    assert store.load_message(session.session_id, message.message_id).content == message.content
    # A transformed/literal request and an unchanged source reference have the same
    # exact bytes; neither stores an encoded copy in its immutable manifest.
    for canonical in (False, True):
        view = publish(store, session, message, canonical=canonical)
        _, body = store.load_latest_request(session.session_id)
        assert len(body.model_dump_json()) < 5000
        assert base64.b64encode(raw).decode() not in body.model_dump_json()
        assert (
            store.load_latest_context(session.session_id).provider_context == view.provider_context
        )
    with pytest.raises(ResourceInUseError, match="canonical message"):
        resources.remove_resource(session.session_id, record.resource_id)


@pytest.mark.parametrize("source_kind", ["upload", "reference"])
def test_media_resource_survives_branch_source_trash_and_cross_backend_transfer(
    services, tmp_path, source_kind
):
    store, files, resources, sessions = services
    session, message, raw = observe(services, tmp_path, source_kind, 8192)
    branch = sessions.branch(session.session_id, message.message_id).session
    branch_message = store.load_session(branch.session_id).messages[0]
    expected = next(part for part in branch_message.content if isinstance(part, MediaContent))
    sessions.trash(session.session_id)
    publish(store, branch, branch_message)
    archive = SessionTransferService(store, files).export((branch.session_id,))
    assert list(archive.files.values()) == [raw]
    target, target_files, _, _ = _services(
        tmp_path / "imported", backend="sqlite" if isinstance(store, LocalSessionStore) else "files"
    )
    SessionTransferService(target, target_files).import_data(archive)
    restored = target.load_session(branch.session_id).messages[0]
    assert next(part for part in restored.content if isinstance(part, MediaContent)) == expected
    latest = target.load_latest_context(branch.session_id)
    assert (
        next(
            part
            for part in latest.provider_context.messages[0].content
            if isinstance(part, MediaContent)
        )
        == expected
    )


@pytest.mark.parametrize("size", [4096, 1024 * 1024])
@pytest.mark.parametrize("omission", ["selected", "capacity", "unsupported"])
async def test_runtime_does_not_read_media_excluded_from_the_final_request(
    services, tmp_path, monkeypatch, size, omission
):
    store, files, resources, _ = services
    session, old, _ = observe(services, tmp_path, "reference", size)
    now = utc_now()
    response = MessageRecord(
        session_id=session.session_id,
        role="assistant",
        model_response=ModelResponseRecord(
            provider_id="test", model_id="test", started_at=now, ended_at=now
        ),
    )
    store.append_message(response)
    source = tmp_path / "new.jpg"
    raw = b"\xff\xd8\xff" + b"new" * (size // 3)
    source.write_bytes(raw)
    newest = resources.prepare_input(
        MessageRecord(
            session_id=session.session_id,
            role="user",
            semantic_tag="attachments",
            input_attachments=(InputFileReference(name=source.name, path=str(source)),),
        ),
        input_modalities=frozenset({"image"}),
        workspace=str(tmp_path),
        workspace_only=True,
    )
    store.append_message(newest)
    source.unlink()
    if omission == "selected":
        current = store.load_session_record(session.session_id)
        store.save_session(
            current.model_copy(
                update={
                    "context_selection": current.context_selection.model_copy(
                        update={
                            "media_elided_through": MessageBoundary(
                                message_id=response.message_id, sequence=response.sequence
                            )
                        }
                    )
                }
            )
        )
    monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", 4 * ((len(raw) + 2) // 3))
    old_id = next(part.resource_id for part in old.content if isinstance(part, MediaContent))
    new_id = next(part.resource_id for part in newest.content if isinstance(part, MediaContent))
    records = {
        record.resource_id: record for record in store.load_resource_references(session.session_id)
    }
    old_name, new_name = records[old_id].filename, records[new_id].filename
    opened = []
    open_path = Path.open

    def track_open(path, mode="r", *args, **kwargs):
        if "resources" in path.parts and "files" in path.parts and "r" in mode and "b" in mode:
            assert path.name != old_name, "elided media was read before request selection"
            assert omission != "unsupported", "unsupported media bytes were loaded"
            opened.append(path.name)
        return open_path(path, mode, *args, **kwargs)

    runtime = RuntimeService(
        store=store,
        resources=resources,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(
                provider_id="test", model_id="test", supports_image_input=omission != "unsupported"
            ),
        ),
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", track_open)
            result = await runtime.run("Continue", session_id=session.session_id)
        assert result.run.status == "completed", result.run.error
        assert opened == ([] if omission == "unsupported" else [new_name])
        context = store.load_latest_context(session.session_id)
        media = [
            part
            for message in context.provider_context.messages
            for part in message.content
            if isinstance(part, MediaContent)
        ]
        assert [part.resource_id for part in media] == (
            [] if omission == "unsupported" else [new_id]
        )
        if media:
            assert base64.b64decode(media[0].uri.partition(",")[2]) == raw
    finally:
        await runtime.close()


def test_missing_foreign_or_unpublished_media_is_rejected_before_message_commit(services, tmp_path):
    store, files, resources, sessions = services
    owner, message, _ = observe(services, tmp_path, "reference", 128)
    block = next(part for part in message.content if isinstance(part, MediaContent))
    other = sessions.create().session
    foreign = MessageRecord(session_id=other.session_id, role="user", content=[block])
    with pytest.raises(ResourceNotFoundError):
        store.append_message(foreign)
    assert store.load_session_record(other.session_id).message_count == 0
    record = store.load_resource_reference(owner.session_id, block.resource_id)
    store.attach_resource_reference(other.session_id, record)
    with pytest.raises(ResourceIntegrityError, match="not published"):
        store.append_message(foreign)
    assert store.load_session_record(other.session_id).message_count == 0


def test_catalog_preview_does_not_materialize_media(services, tmp_path, monkeypatch):
    store, _, _, _ = services
    session, message, _ = observe(services, tmp_path, "reference", 8192)
    media = next(part for part in message.content if isinstance(part, MediaContent))
    latest = MessageRecord.text(session_id=session.session_id, role="assistant", text="preview")
    latest.content.append(media)
    store.append_message(latest)

    def forbidden(path):
        pytest.fail("catalog preview loaded binary media")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert store.refresh_catalog(session.session_id) is None
    assert store.read_catalog().sessions[0].last_message_preview == "preview"


def test_observation_publication_failure_is_not_an_unavailable_file_fallback(
    services, tmp_path, monkeypatch
):
    store, files, resources, sessions = services
    session = sessions.create().session
    raw = b"\xff\xd8\xffimmutable observed bytes"
    path = tmp_path / "picture.jpg"
    path.write_bytes(raw)
    original = store.attach_resource_reference
    published = []

    def fail_after_metadata(session_id, record):
        original(session_id, record)
        published.append(record)
        raise OSError("metadata acknowledgement lost")

    monkeypatch.setattr(store, "attach_resource_reference", fail_after_metadata)
    with pytest.raises(OSError, match="acknowledgement lost"):
        resources.prepare_input(
            MessageRecord(
                session_id=session.session_id,
                role="user",
                input_attachments=(InputFileReference(name=path.name, path=str(path)),),
            ),
            input_modalities=frozenset({"image"}),
            workspace=str(tmp_path),
            workspace_only=True,
        )
    assert store.load_session_record(session.session_id).message_count == 0
    assert files.load_content(session.session_id, published[0]) == raw
    assert (
        store.load_resource_reference(session.session_id, published[0].resource_id) == published[0]
    )


@pytest.mark.parametrize("query", ["message", "page", "request"])
@pytest.mark.parametrize("removal", ["withdraw", "trash", "replace"])
def test_media_read_owns_bytes_across_concurrent_removal(
    services, tmp_path, monkeypatch, query, removal
):
    store, files, resources, sessions = services
    session, message, raw = observe(services, tmp_path, "reference", 4096)
    media = next(part for part in message.content if isinstance(part, MediaContent))
    blob = resources.resource_path(session.session_id, media.resource_id)
    other = sessions.create().session
    publish(store, session, message, canonical=False)
    archive = (
        SessionTransferService(store, files).export((session.session_id,))
        if removal == "replace"
        else None
    )
    entered, release, removal_waiting = Event(), Event(), Event()
    original_read = Path.read_bytes
    original_acquire = FileMutationLock.acquire

    def read(path):
        if path == blob:
            entered.set()
            assert release.wait(10)
        return original_read(path)

    def acquire(lock, *args, **kwargs):
        if entered.is_set() and not release.is_set():
            removal_waiting.set()
        return original_acquire(lock, *args, **kwargs)

    def get_media():
        if query == "message":
            values = [store.load_message(session.session_id, message.message_id)]
        elif query == "page":
            values, _ = store.load_messages_page(session.session_id)
        else:
            values = store.load_latest_context(session.session_id).provider_context.messages
        return next(
            part for item in values for part in item.content if isinstance(part, MediaContent)
        )

    def remove():
        if removal == "trash":
            sessions.trash(session.session_id)
        elif removal == "replace":
            SessionTransferService(store, files).import_data(archive, replace=True)
        else:
            store.truncate_messages(
                session.session_id,
                before_message_id=message.message_id,
                expected_last_message_id=message.message_id,
            )
            # A literal request does not depend on the withdrawn message. Replace
            # it explicitly before deleting its last resource reference.
            publish(
                store,
                session,
                MessageRecord.text(session_id=session.session_id, role="user", text="next"),
                canonical=False,
            )
            resources.remove_resource(session.session_id, media.resource_id)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(FileMutationLock, "acquire", acquire)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(get_media)
        deleting = None
        try:
            assert entered.wait(10)
            deleting = pool.submit(remove)
            assert removal_waiting.wait(10)
            store.append_message(
                MessageRecord.text(session_id=other.session_id, role="user", text="unrelated write")
            )
            assert not reading.done() and not deleting.done()
        finally:
            release.set()
        actual = reading.result(timeout=10)
        assert deleting is not None
        deleting.result(timeout=10)
    assert base64.b64decode(actual.uri.partition(",")[2]) == raw
    assert blob.exists() is (removal == "replace")
