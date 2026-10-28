from __future__ import annotations

import base64
import sqlite3
from contextlib import closing

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.application.transfer.sessions import SessionTransferService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.content import ImageContent, MediaContent
from ouro.core.context import ContextBuilder
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.messages import MessageRecord, ModelMessage
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommit
from ouro.core.state import SessionRecord
from ouro.providers import FauxProvider
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.sessions.test_request_manifest import store as store


def literal(index, size):
    encoded = base64.b64encode(f"generation:{index}:".encode() + b"x" * size).decode()
    return ImageContent(uri=f"data:image/png;base64,{encoded}", media_type="image/png")


@pytest.mark.parametrize("size", [4096, 1024 * 1024])
@pytest.mark.parametrize("replacements", [2, 32])
def test_request_only_media_is_owned_once_and_replaced_without_resource_accumulation(
    store, size, replacements
):
    session = SessionRecord()
    store.create_session(session)
    first = None
    for index in range(replacements):
        media = literal(index, size)
        view = ContextBuilder().build(
            messages=[ModelMessage(role="user", content=[media])], tools=[]
        )
        header = ModelRequestHeader(
            session_id=session.session_id,
            run_id="run",
            turn_id=f"turn_{index}",
            model_round=index + 1,
            provider_id="test",
            model_id="test",
            metrics=view.metrics,
        )
        store.commit_session(
            SessionCommit(
                before=store.load_session_record(session.session_id),
                request_header=header,
                request_manifest=RequestManifestRecord(
                    context_id=header.context_id, manifest=view.manifest
                ),
            )
        )
        if first is None:
            first = store.load_latest_context(session.session_id)
    assert first.provider_context.messages[0].content == [literal(0, size)]
    assert store.load_latest_context(session.session_id).provider_context == view.provider_context
    assert store.load_resource_references(session.session_id) == []
    assert store.load_messages_tail(session.session_id) == []
    if isinstance(store, LocalSessionStore):
        directory = store.sessions_dir / session.session_id
        files = list((directory / "request-content").iterdir())
        assert [path.name for path in files] == [f"{header.context_id}.json"]
        payload = files[0].read_text(encoding="utf-8")
        with closing(sqlite3.connect(directory / "session-data.sqlite3")) as connection:
            headers = [row[0] for row in connection.execute("SELECT payload FROM request_headers")]
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            rows = connection.execute("SELECT payload FROM latest_contexts").fetchall()
            assert len(rows) == 1
            payload = rows[0][0]
            headers = [row[0] for row in connection.execute("SELECT payload FROM request_headers")]
    assert payload.count(media.uri) == 1
    assert len(payload) < len(media.uri) + 1500
    assert len(headers) == replacements
    assert all(len(value) < 2000 and "base64" not in value for value in headers)
    response = MessageRecord.text(session_id=session.session_id, role="assistant", text="answer")
    store.append_message(response)
    store.truncate_messages(
        session.session_id,
        before_message_id=response.message_id,
        expected_last_message_id=response.message_id,
    )
    assert store.load_latest_context(session.session_id) is None
    if isinstance(store, LocalSessionStore):
        assert list((directory / "request-content").iterdir()) == []
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            assert connection.execute("SELECT count(*) FROM latest_contexts").fetchone() == (0,)


async def test_dynamic_media_preparation_is_shared_with_adapter_and_never_replayed_on_query(
    store, tmp_path
):
    class Builder(ContextBuilder):
        calls = 0

        def build(self, *, messages, **kwargs):
            self.calls += 1
            return super().build(
                messages=[
                    *messages,
                    ModelMessage(role="user", content=[literal(self.calls, 4096)]),
                ],
                **kwargs,
            )

    class Provider(FauxProvider):
        def __init__(self):
            super().__init__()
            self.media = []

        async def stream(self, **kwargs):
            self.media.append(
                [
                    part
                    for message in kwargs["messages"]
                    for part in message.content
                    if isinstance(part, MediaContent)
                ]
            )
            async for event in super().stream(**kwargs):
                yield event

    builder, provider = Builder(), Provider()
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        context_builder=builder,
        adapter=RuntimeAdapter(
            provider, ModelMetadata(provider_id="test", model_id="test", supports_image_input=True)
        ),
    )
    try:
        first = await runtime.run("First")
        assert first.run.status == "completed", first.run.error
        saved = store.load_latest_context(first.session.session_id)
        assert saved.provider_context.messages[-1].content == provider.media[0]
        assert builder.calls == 1
        second = await runtime.run("Next", session_id=first.session.session_id)
        assert second.run.status == "completed", second.run.error
        latest = store.load_latest_context(first.session.session_id)
        assert latest.provider_context.messages[-1].content == provider.media[1]
        assert provider.media[0] != provider.media[1]
        assert saved.provider_context.messages[-1].content == provider.media[0]
        assert builder.calls == 2
        assert store.load_resource_references(first.session.session_id) == []
        root = store.root if isinstance(store, LocalSessionStore) else store.database.parent
        archive = SessionTransferService(store, LocalResourceStore(root)).export(
            (first.session.session_id,)
        )
        assert archive.files == {}
        target_root = tmp_path / "target"
        target = (
            SQLiteSessionStore(target_root / "sessions.db")
            if isinstance(store, LocalSessionStore)
            else LocalSessionStore(target_root)
        )
        target.initialize()
        SessionTransferService(target, LocalResourceStore(target_root)).import_data(archive)
        assert target.load_latest_context(first.session.session_id) == latest
    finally:
        await runtime.close()
