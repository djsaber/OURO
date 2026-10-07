from __future__ import annotations

from ouro.application.resources import ResourceService
from ouro.application.runtime import RuntimeService
from ouro.application.sessions import SessionService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata
from ouro.providers import FauxProvider
from ouro.resources import LocalResourceStore
from ouro.sessions import SQLiteSessionStore
from ouro.tools import ToolRegistry


async def test_runtime_executes_against_sqlite_session_store(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "data" / "ouro.db")
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
    )

    result = await runtime.run("hello from sqlite")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "completed"
    assert [message.role for message in snapshot.messages] == ["user", "assistant"]
    assert snapshot.messages[0].text_content == "hello from sqlite"
    assert snapshot.events[-1].event_type == "run_completed"


def test_sqlite_sessions_compose_with_local_blob_store(tmp_path) -> None:
    sessions = SQLiteSessionStore(tmp_path / "data" / "ouro.db")
    sessions.initialize()
    blobs = LocalResourceStore(tmp_path / "data")
    blobs.initialize()
    resources = ResourceService(store=blobs, sessions=sessions)
    service = SessionService(sessions, resources=resources)
    source_session = service.create(title="SQLite resources").session
    source = tmp_path / "attachment.txt"
    source.write_text("portable blob", encoding="utf-8")

    imported = resources.import_resource(source_session.session_id, source)
    message = MessageRecord.text(
        session_id=source_session.session_id,
        role="user",
        text="branch this",
        sequence=1,
    ).model_copy(update={"resource_refs": [imported.resource.resource_id]})
    sessions.append_message(message)
    branch = service.branch(source_session.session_id, message.message_id)
    copied = service.create(title="SQLite copied references").session
    resources.copy_references(
        source_session.session_id,
        copied.session_id,
        (imported.resource.resource_id,),
    )

    assert resources.session_resource_ids(source_session.session_id) == (
        imported.resource.resource_id,
    )
    assert resources.session_resource_ids(branch.session.session_id) == (
        imported.resource.resource_id,
    )
    copied_reference = sessions.load_resource_references(copied.session_id)[0]
    assert copied_reference.copied_from_session_id == source_session.session_id
    service.trash(copied.session_id)
    service.trash(branch.session.session_id)
    source_path = resources.resource_path(source_session.session_id, imported.resource.resource_id)
    assert source_path.read_bytes() == source.read_bytes()
    result = service.trash(source_session.session_id)
    assert not source_path.exists()
    retired = (
        tmp_path
        / "data"
        / "trash"
        / "sessions"
        / result.trash_id
        / source_session.session_id
        / "resources"
        / "files"
        / source.name
    )
    assert retired.read_bytes() == source.read_bytes()
