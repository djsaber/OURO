from __future__ import annotations

import pytest

from ouro.application.inputs.queue import input_operation_id
from ouro.application.resources import ResourceService
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id
from ouro.core.errors import PersistenceError
from ouro.core.input_content import InputAttachment
from ouro.core.provider import ModelMetadata
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


def application_at(root, *, backend="files"):
    store = (
        LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "sessions.db")
    )
    store.initialize()
    resources = LocalResourceStore(root)
    resources.initialize()
    return LocalApplication.from_runtime(
        RuntimeService(
            store=store,
            adapter=RuntimeAdapter(
                FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")
            ),
            tools=ToolRegistry(),
            resources=ResourceService(store=resources, sessions=store),
            environment=RuntimeEnvironment.local(root),
        )
    )


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_prepared_upload_becomes_first_input_without_reimport(tmp_path, backend, monkeypatch):
    application = application_at(tmp_path / "home", backend=backend)
    runtime = application.runtime
    resources = runtime.resources
    identity = resources.reserve()
    source = tmp_path / "notes.txt"
    source.write_text("prepared material")
    record = resources.import_prepared_resource(identity, source).resource
    attachment = InputAttachment(resource_id=record.resource_id, name=source.name)
    file = resources.directory_for(identity) / record.filename
    original_stat = file.stat()
    source.unlink()
    assert runtime.store.list_sessions() == []

    def unexpected_import(*_args, **_kwargs):
        pytest.fail("Prepared input must not reimport its files")

    monkeypatch.setattr(resources._store, "import_file", unexpected_import)
    accepted = []
    content_reads = []
    open_path = type(file).open

    def track_open(path, mode="r", *args, **kwargs):
        if path == file and "b" in mode:
            content_reads.append(path)
        return open_path(path, mode, *args, **kwargs)

    def input_accepted():
        assert content_reads == [], "Adoption must not add a complete file read"
        accepted.append(True)

    monkeypatch.setattr(type(file), "open", track_open)
    result = await runtime.run(
        "Read this",
        new_session_id=identity,
        input_id="first-input",
        attachments=(attachment,),
        on_input_accepted=input_accepted,
    )
    assert result.run.status == "completed"
    assert accepted == [True]
    assert result.session.session_id == identity
    assert runtime.sessions.input_accepted(identity, "first-input")
    assert len(runtime.store.list_sessions()) == 1
    assert resources.resource_path(identity, record.resource_id) == file
    assert file.stat().st_ino == original_stat.st_ino
    assert file.stat().st_mtime_ns == original_stat.st_mtime_ns
    messages = runtime.store.load_messages_tail(identity)
    assert sum(m.semantic_tag == "attachments" for m in messages) == 1
    assert any("prepared material" in m.text_content for m in messages)
    resources.release_prepared(identity)
    assert file.read_text() == "prepared material"
    await application.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
@pytest.mark.parametrize("edited", (False, True))
async def test_interrupted_publication_keeps_preparation_retryable(
    tmp_path, backend, edited, monkeypatch
):
    application = application_at(tmp_path / "home", backend=backend)
    runtime = application.runtime
    identity = runtime.resources.reserve()
    source = tmp_path / "notes.txt"
    source.write_text("survives publication failure")
    record = runtime.resources.import_prepared_resource(identity, source).resource
    attachment = InputAttachment(resource_id=record.resource_id, name=source.name)

    def fail(*_args, **_kwargs):
        raise PersistenceError("publication interrupted")

    with monkeypatch.context() as patch:
        if backend == "files":
            patch.setattr(runtime.store._commits, "save_session_record", fail)
        else:
            patch.setattr(runtime.store, "_insert_resource_reference", fail)
        with pytest.raises(PersistenceError, match="publication interrupted"):
            await runtime.run(
                "read",
                new_session_id=identity,
                input_id="interrupted",
                attachments=(attachment,),
            )
    assert runtime.store.list_sessions() == []
    assert not runtime.sessions.input_accepted(identity, "interrupted")
    assert runtime.resources.prepared_references(identity)[0].resource_id == record.resource_id
    retry_id = "retry" if edited else "interrupted"
    result = await runtime.run(
        "edited" if edited else "read",
        new_session_id=identity,
        input_id=retry_id,
        attachments=(attachment,),
    )
    assert result.run.status == "completed"
    assert len(runtime.store.list_sessions()) == 1
    assert runtime.sessions.input_accepted(identity, retry_id)
    assert runtime.sessions.input_accepted(identity, "interrupted") == (not edited)
    assert (
        sum(m.semantic_tag == "attachments" for m in runtime.store.load_messages_tail(identity))
        == 1
    )
    await application.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_existing_session_acceptance_receipt_survives_history_undo(tmp_path, backend):
    application = application_at(tmp_path, backend=backend)
    runtime = application.runtime
    session = runtime.create_session()
    result = await runtime.run("submitted", session_id=session.session_id, input_id="sent")
    assert result.run.status == "completed"
    assert runtime.sessions.input_accepted(session.session_id, "sent")
    runtime.sessions.undo(session.session_id)
    assert runtime.sessions.input_accepted(session.session_id, "sent")
    assert not runtime.sessions.input_accepted(session.session_id, "unsent")
    await application.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_first_text_input_needs_no_resource_reservation(tmp_path, backend, monkeypatch):
    application = application_at(tmp_path, backend=backend)
    runtime = application.runtime
    identity = new_id("ses")

    def unexpected_reservation(*_args, **_kwargs):
        pytest.fail("Text-only input must not reserve file storage")

    monkeypatch.setattr(runtime.resources, "reserve", unexpected_reservation)
    result = await runtime.run("hello", new_session_id=identity, input_id="text-input")
    assert result.session.session_id == identity
    assert result.run.status == "completed"
    assert runtime.resources.prepared_references(identity) is None
    assert runtime.sessions.input_accepted(identity, "text-input")
    runtime.sessions.undo(identity)
    assert runtime.sessions.input_accepted(identity, "text-input")
    await application.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_prepared_input_rejection_keeps_resources_without_session(
    tmp_path, backend, monkeypatch
):
    application = application_at(tmp_path / "home", backend=backend)
    runtime = application.runtime
    identity = runtime.resources.reserve()
    source = tmp_path / "notes.txt"
    source.write_text("retained")
    record = runtime.resources.import_prepared_resource(identity, source).resource

    async def reject(**_kwargs):
        raise ValueError("Input policy rejected")

    monkeypatch.setattr(runtime, "_evaluate_prompt_policies", reject)
    with pytest.raises(ValueError, match="Input policy rejected"):
        await runtime.run(
            "read",
            new_session_id=identity,
            input_id="rejected",
            attachments=(InputAttachment(resource_id=record.resource_id, name=source.name),),
        )
    assert runtime.store.list_sessions() == []
    assert not runtime.sessions.input_accepted(identity, "rejected")
    assert runtime.resources.prepared_references(identity)[0].resource_id == record.resource_id
    runtime.resources.release_prepared(identity)
    assert not runtime.resources.directory_for(identity).exists()
    assert source.read_text() == "retained"
    await application.close()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
@pytest.mark.parametrize("prepared", (False, True))
async def test_first_input_acceptance_survives_execution_setup_failure(
    tmp_path, backend, prepared, monkeypatch
):
    application = application_at(tmp_path, backend=backend)
    runtime = application.runtime
    identity = runtime.resources.reserve() if prepared else None

    def fail(_session):
        raise PersistenceError("execution setup failed")

    monkeypatch.setattr(runtime, "_resolve_session_adapter", fail)
    accepted = []
    created = []
    with pytest.raises(PersistenceError, match="execution setup failed"):
        await runtime.run(
            "keep this",
            new_session_id=identity,
            input_id="accepted",
            on_session_created=created.append,
            on_input_accepted=lambda: accepted.append(True),
        )
    assert accepted == [True]
    identity = created[0].session_id
    assert runtime.sessions.input_accepted(identity, "accepted")
    assert runtime.store.load_commit(identity, input_operation_id("accepted")) is not None
    session = runtime.store.load_runtime_session(identity)
    queued = runtime.queued_inputs.eligible(session)
    assert len(queued) == 1 and queued[0].prompt == "keep this"
    assert session.messages == []
    await application.close()
