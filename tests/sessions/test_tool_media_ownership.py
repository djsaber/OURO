from __future__ import annotations

import base64
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.application.resources import ResourceService
from ouro.application.sessions.service import SessionService
from ouro.application.transfer.sessions import SessionTransferService
from ouro.core.content import (
    AudioContent,
    ImageContent,
    MediaContent,
    TextContent,
    VideoContent,
    media_data_uri,
)
from ouro.core.errors import (
    PersistenceError,
    ResourceInUseError,
    ResourceNotFoundError,
    SessionRecoveryRequiredError,
)
from ouro.core.messages import MessageRecord, ToolMessageReference
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.tool_facts import ToolCallFacts, ToolFactConflict
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.session_storage import session_connection
from tests.sessions.test_tool_fact_storage import commit, facts, file_fact_payload
from tests.sessions.test_tool_fact_storage import store as store


def image_outcome(outcome):
    return outcome.model_copy(
        update={
            "value": outcome.value.model_copy(
                update={"content": [ImageContent(uri="data:image/png;base64,YQ==")]}
            )
        }
    )


@pytest.mark.parametrize(
    "kind,constructor,mime",
    [
        ("image", ImageContent, "image/png"),
        ("audio", AudioContent, "audio/wav"),
        ("video", VideoContent, "video/mp4"),
    ],
)
@pytest.mark.parametrize("size", [4096, 1024 * 1024])
def test_tool_media_is_one_owned_resource_for_both_outcome_commit_paths(
    store, kind, constructor, mime, size
):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    raw = b"observed-media" * (size // 14)
    uri = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    media = constructor(uri=uri, media_type=mime if kind != "image" else "")
    outcome = outcome.model_copy(
        update={
            "value": outcome.value.model_copy(
                update={
                    "content": [TextContent(text="observed"), media],
                }
            )
        }
    )
    # Independent observation first; a repeated ordinary batch commit must reuse
    # the original identity rather than publish another media file.
    saved = store.commit_tool_outcome(outcome)
    commit(store, session, outcomes=(outcome,))
    commit(store, session, outcomes=(saved,))
    loaded = store.load_tool_outcome(session.session_id, call.identity, 1)
    assert loaded.value.same_observation(outcome.value)
    blocks = [part for part in loaded.value.content if isinstance(part, MediaContent)]
    assert blocks[0].uri == uri
    records = store.load_resource_references(session.session_id)
    assert len(records) == 1 and records[0].internal
    assert blocks[0].resource_id == records[0].resource_id
    root = store.root if isinstance(store, LocalSessionStore) else store.database.parent
    resources = LocalResourceStore(root)
    assert resources.load_content(session.session_id, records[0]) == raw
    if isinstance(store, LocalSessionStore):
        payload = file_fact_payload(store.sessions_dir / session.session_id, outcome)
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            payload = connection.execute("SELECT payload FROM tool_outcomes").fetchone()[0]
    assert len(payload) < 1800 and base64.b64encode(raw).decode() not in payload
    assert store.load_messages_tail(session.session_id) == []
    assert store.tool_outcomes_use_resource(session.session_id, records[0].resource_id)
    with pytest.raises(ToolFactConflict, match="cannot be replaced"):
        store.commit_tool_outcome(
            outcome.model_copy(
                update={
                    "value": outcome.value.model_copy(
                        update={"content": [constructor(uri=uri + "changed", media_type=mime)]}
                    )
                }
            )
        )
    assert len(store.load_resource_references(session.session_id)) == 1


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("data:image/png;charset=binary;base64,AAH/", b"\x00\x01\xff"),
        ("data:image/png,%00%01%ff", b"\x00\x01\xff"),
        ("data:image/png;base64,AAH%2F", b"\x00\x01\xff"),
    ],
)
def test_data_uri_capture_preserves_observed_bytes_and_header(store, uri, expected):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    outcome = outcome.model_copy(
        update={
            "value": outcome.value.model_copy(
                update={
                    "content": [ImageContent(uri=uri)],
                }
            )
        }
    )
    commit(store, session, outcomes=(outcome,))
    loaded = store.load_tool_outcome(session.session_id, call.identity, 1)
    assert loaded.value.same_observation(outcome.value)
    assert media_data_uri(loaded.value.content[0].uri)[1] == expected
    assert len(store.load_resource_references(session.session_id)) == 1


@pytest.mark.parametrize("uri", ["https://media.example/picture.png", "data:image/png;base64,!!!!"])
def test_remote_or_invalid_opaque_uri_is_retained_without_inventing_bytes(store, uri):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    outcome = outcome.model_copy(
        update={"value": outcome.value.model_copy(update={"content": [ImageContent(uri=uri)]})}
    )
    assert store.commit_tool_outcome(outcome) == outcome
    assert store.load_resource_references(session.session_id) == []


def test_late_media_after_withdrawal_keeps_resource_without_reopening_history(store):
    session, run, call, start, outcome = facts(store)
    message = MessageRecord.text(session_id=session.session_id, role="assistant", text="in flight")
    store.append_message(message)
    commit(store, session, calls=(call,), starts=(start,))
    store.save_run(run.model_copy(update={"status": "aborted"}))
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            before_message_id=message.message_id,
        )
    )
    before = store.load_session_record(session.session_id)
    outcome = outcome.model_copy(
        update={
            "value": outcome.value.model_copy(
                update={"content": [ImageContent(uri="data:image/png;base64,YQ==")]}
            )
        }
    )
    saved = store.commit_tool_outcome(outcome)
    assert store.load_session_record(session.session_id) == before.model_copy(
        update={"resource_count": before.resource_count + 1}
    )
    assert store.load_messages_tail(session.session_id) == []
    assert saved.value.same_observation(outcome.value)
    record = store.load_resource_references(session.session_id)[0]
    root = store.root if isinstance(store, LocalSessionStore) else store.database.parent
    resources = ResourceService(store=LocalResourceStore(root), sessions=store)
    with pytest.raises(ResourceInUseError, match="tool Outcome"):
        resources.remove_resource(session.session_id, record.resource_id)


def test_foreign_media_id_in_independent_outcome_is_rejected(store):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    outcome = outcome.model_copy(
        update={
            "value": outcome.value.model_copy(
                update={
                    "content": [
                        ImageContent(
                            uri="data:image/png;base64,YQ==", resource_id="res_" + "a" * 32
                        )
                    ]
                }
            )
        }
    )
    with pytest.raises(ResourceNotFoundError):
        store.commit_tool_outcome(outcome)
    assert store.load_tool_outcome(session.session_id, call.identity, 1) is None


def test_tool_media_branch_and_transfer_keep_reference_only_outcome(store, tmp_path):
    session, run, call, start, outcome = facts(store)
    outcome = image_outcome(outcome)
    commit(store, session, calls=(call,), starts=(start,), outcomes=(outcome,))
    assistant = MessageRecord(
        session_id=session.session_id, role="assistant", call_refs=(call.identity,)
    )
    result = MessageRecord(
        session_id=session.session_id,
        role="tool_result",
        outcome_ref=ToolMessageReference(identity=call.identity, outcome_attempt=1),
    )
    before = store.load_session_record(session.session_id)
    store.commit_session(
        SessionCommit(
            before=before,
            after=before.model_copy(update={"active_run_id": None, "status": "idle"}),
            runs=(run.model_copy(update={"status": "completed"}),),
            steps=(SessionCommitStep(message=assistant), SessionCommitStep(message=result)),
        )
    )
    root = store.root if isinstance(store, LocalSessionStore) else store.database.parent
    files = LocalResourceStore(root)
    sessions = SessionService(store, resources=ResourceService(store=files, sessions=store))
    branch = sessions.branch(session.session_id, result.message_id).session
    sessions.trash(session.session_id)
    snapshot = store.load_session(branch.session_id)
    assert snapshot.tool_facts.outcomes[0].value.content[0].uri == ""
    expected = snapshot.messages[-1].content
    assert expected[0].uri == "data:image/png;base64,YQ=="
    archive = SessionTransferService(store, files).export((branch.session_id,))
    assert list(archive.files.values()) == [b"a"]
    target_root = tmp_path / "imported"
    target = (
        SQLiteSessionStore(target_root / "sessions.db")
        if isinstance(store, LocalSessionStore)
        else LocalSessionStore(target_root)
    )
    target.initialize()
    SessionTransferService(target, LocalResourceStore(target_root)).import_data(archive)
    restored = target.load_session(branch.session_id)
    assert restored.messages[-1].content == expected
    assert restored.tool_facts == snapshot.tool_facts


def test_snapshot_rejects_inline_outcome_bytes(store):
    session, _, call, start, outcome = facts(store)
    with pytest.raises(ToolFactConflict, match="must reference its Resource"):
        store.create_session_snapshot(
            session,
            [],
            tool_facts=ToolCallFacts(
                calls=(call,), starts=(start,), outcomes=(image_outcome(outcome),)
            ),
        )


def test_native_outcome_read_rejects_an_inline_media_copy(store):
    session, _, call, start, outcome = facts(store)
    outcome = image_outcome(outcome)
    commit(store, session, calls=(call,), starts=(start,), outcomes=(outcome,))
    payload = outcome.model_dump_json()
    if isinstance(store, LocalSessionStore):
        file_fact_payload(store.sessions_dir / session.session_id, outcome, payload)
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("UPDATE tool_outcomes SET payload = ?", (payload,))
    with pytest.raises(ToolFactConflict, match="must reference its Resource"):
        store.load_tool_outcome(session.session_id, call.identity, 1)
    with pytest.raises(ToolFactConflict, match="must reference its Resource"):
        store.load_session(session.session_id)


def test_context_tail_resolves_outcome_identity_without_loading_media(store, monkeypatch):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,), outcomes=(image_outcome(outcome),))
    result = MessageRecord(
        session_id=session.session_id,
        role="tool_result",
        outcome_ref=ToolMessageReference(identity=call.identity, outcome_attempt=1),
    )
    store.append_message(result)

    def forbid_bytes(*args, **kwargs):
        pytest.fail("context tail loaded media before selection")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", forbid_bytes)
        selected = store.load_messages_tail(session.session_id, include_media=False)
    assert selected[0].tool_result.tool_call_id == call.identity.tool_call_id
    assert selected[0].content[0].resource_id is not None
    assert selected[0].content[0].uri == ""
    assert store.load_messages_tail(session.session_id)[0].content[0].uri == (
        "data:image/png;base64,YQ=="
    )


def test_tool_media_native_failure_preserves_published_bytes(store):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    outcome = image_outcome(outcome)
    before = store.load_session_record(session.session_id)
    with session_connection(store, session.session_id) as connection:
        connection.execute(
            "CREATE TRIGGER fail_media AFTER INSERT ON resource_references "
            "BEGIN SELECT RAISE(ABORT, 'injected media Outcome interruption'); END"
        )
    with pytest.raises(PersistenceError, match="injected media Outcome"):
        store.commit_tool_outcome(outcome)
    assert store.load_tool_outcome(session.session_id, call.identity, 1) is None
    assert store.load_resource_references(session.session_id) == []
    assert store.load_session_record(session.session_id) == before
    if isinstance(store, LocalSessionStore):
        assert not (store.sessions_dir / session.session_id / "commit.json").exists()
    with session_connection(store, session.session_id) as connection:
        connection.execute("DROP TRIGGER fail_media")
    # Recommit the already observed value; the external operation is not rerun.
    saved = store.commit_tool_outcome(outcome)
    assert saved.value.same_observation(outcome.value)
    assert store.load_session_record(session.session_id) == before.model_copy(
        update={"resource_count": before.resource_count + 1}
    )
    assert store.load_tool_call_start(session.session_id, call.identity, 1) == start
    records = store.load_resource_references(session.session_id)
    assert len(records) == 1
    root = store.root if isinstance(store, LocalSessionStore) else store.database.parent
    assert LocalResourceStore(root).load_content(session.session_id, records[0]) == b"a"


def test_media_and_message_share_recoverable_publication(store):
    session, _, call, start, outcome = facts(store)
    commit(store, session, calls=(call,), starts=(start,))
    outcome = image_outcome(outcome)
    message = MessageRecord(
        session_id=session.session_id,
        role="tool_result",
        outcome_ref=ToolMessageReference(identity=call.identity, outcome_attempt=1),
    )
    request = SessionCommit(
        before=store.load_session_record(session.session_id),
        tool_facts=ToolCallFacts(outcomes=(outcome,)),
        steps=(SessionCommitStep(message=message),),
    )
    with session_connection(store, session.session_id) as connection:
        connection.execute(
            "CREATE TRIGGER fail_media AFTER INSERT ON resource_references "
            "BEGIN SELECT RAISE(ABORT, 'media publication interrupted'); END"
        )
    with pytest.raises(PersistenceError, match="media publication interrupted"):
        store.commit_session(request)
    with session_connection(store, session.session_id) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_outcomes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM resource_references").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_media")
    if isinstance(store, LocalSessionStore):
        with pytest.raises(SessionRecoveryRequiredError):
            store.load_resource_references(session.session_id)
        with store.session_mutation(session.session_id):
            pass
    else:
        store.commit_session(request)
    assert store.load_session_record(session.session_id).resource_count == 1
    assert len(store.load_resource_references(session.session_id)) == 1
    assert store.load_message(session.session_id, message.message_id).content[0].uri == (
        "data:image/png;base64,YQ=="
    )
