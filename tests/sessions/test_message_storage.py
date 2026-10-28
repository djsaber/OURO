from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

import ouro.local_io as local_io
import ouro.sessions.backends.files.commits as file_commits
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import InputMessageReference, MessageFact, MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files.commits import FileSessionCommits
from ouro.sessions.backends.files.message_locations import FileMessageIndex
from tests.input_facts import append_input_messages, input_snapshot, message_request
from tests.session_storage import session_connection


def message(session_id, identity="msg_selected", input_id="queue_selected"):
    return MessageRecord.text(
        session_id=session_id, role="user", text="unique message body", run_id="run_selected"
    ).model_copy(
        update={"message_id": identity, "input_ref": InputMessageReference(input_id=input_id)}
    )


def test_packed_message_offsets_follow_utf8_bytes_across_snapshot_append_and_undo(tmp_path):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    messages = [
        MessageRecord.text(
            session_id=session.session_id, role="assistant", text=text, sequence=index + 1
        ).model_copy(update={"before_harness_version": session.harness_version})
        for index, text in enumerate(("中文🙂", "naïve\n正文", "最后一条"))
    ]
    store.create_session_snapshot(session, messages[:2])
    store.append_message(messages[2])
    cursor = 0
    for expected in messages:
        page, next_cursor = store.load_messages_page(session.session_id, cursor=cursor, limit=1)
        assert page == [store.load_message(session.session_id, expected.message_id)]
        assert page[0].text_content == expected.text_content
        if next_cursor is not None:
            cursor = next_cursor
    assert next_cursor is None
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[1].message_id,
        expected_last_message_id=messages[2].message_id,
    )
    store.append_message(messages[2])
    cold = LocalSessionStore(tmp_path)
    assert [item.text_content for item in cold.load_messages_page(session.session_id)[0]] == [
        messages[0].text_content,
        messages[2].text_content,
    ]
    assert cold.load_message(session.session_id, messages[2].message_id).text_content == "最后一条"


@pytest.mark.parametrize("backend", [LocalSessionStore, SQLiteSessionStore])
def test_storage_reads_transfer_private_facts_without_serializing_them(
    tmp_path, monkeypatch, backend
):
    store = backend(tmp_path if backend is LocalSessionStore else tmp_path / "session.db")
    store.initialize()
    session = SessionRecord()
    original = MessageRecord.text(session_id=session.session_id, role="assistant", text="owned")
    store.create_session_snapshot(session, [original])

    def reject_serialization(*args, **kwargs):
        pytest.fail("reading a validated private fact must not serialize its body again")

    monkeypatch.setattr(MessageFact, "model_dump", reject_serialization)
    monkeypatch.setattr(MessageFact, "model_dump_json", reject_serialization)
    monkeypatch.setattr(MessageRecord, "model_validate", reject_serialization)
    selected = store.load_message(session.session_id, original.message_id)
    assert type(selected) is MessageRecord
    selected.content[0].text = "changed view"
    observed = store.load_session_observation(session.session_id)
    assert observed.messages[0].text_content == "owned"
    observed.messages[0].content[0].text = "changed observation"
    assert store.load_messages_tail(session.session_id)[0].text_content == "owned"


@pytest.mark.parametrize("backend", [LocalSessionStore, SQLiteSessionStore])
def test_snapshot_encoding_does_not_construct_intermediate_message_facts(
    tmp_path, monkeypatch, backend
):
    store = backend(tmp_path if backend is LocalSessionStore else tmp_path / "session.db")
    store.initialize()
    session = SessionRecord()
    original = MessageRecord.text(session_id=session.session_id, role="assistant", text="owned")
    original.metadata = {"nested": {"keep": None, "empty": []}}

    def reject_projection(*args, **kwargs):
        pytest.fail("encoding typed content must not create another full message model")

    monkeypatch.setattr(MessageRecord, "as_fact", reject_projection)
    store.create_session_snapshot(session, [original])
    original.content[0].text = "caller changed"
    original.metadata["nested"]["empty"].append("caller changed")
    loaded = store.load_message(session.session_id, original.message_id)
    assert loaded.text_content == "owned"
    assert loaded.metadata == {"nested": {"keep": None, "empty": []}}


def test_normal_file_commit_does_not_read_back_its_accepted_decision(tmp_path, monkeypatch):
    import ouro.sessions.backends.files.commits as file_commits

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    read = file_commits.read_text_atomic

    def read_existing(path):
        assert path.name != "commit.json", "only recovery needs to decode an accepted decision"
        return read(path)

    monkeypatch.setattr(file_commits, "read_text_atomic", read_existing)
    request = message_request(session, [message(session.session_id)])
    store.commit_session(request)
    assert (
        store.load_message(session.session_id, "msg_selected").text_content == "unique message body"
    )


def assert_pending_reads(store, session_id, message_id):
    decision = store.sessions_dir / session_id / "commit.json"
    payload = decision.read_bytes()
    for query in (
        lambda: store.load_message(session_id, message_id),
        lambda: store.load_messages_page(session_id),
        lambda: store.load_messages_tail(session_id),
        lambda: list(store.iter_messages_before(session_id)),
        lambda: store.load_run_messages(session_id, "run_selected"),
        lambda: store.find_queued_input_message(session_id, "queue_selected"),
    ):
        with pytest.raises(SessionRecoveryRequiredError):
            query()
        assert decision.read_bytes() == payload


@pytest.mark.parametrize(
    "boundary",
    [
        "index_uncommitted",
        "index_committed",
        "journal",
        "partial_journal",
        "head",
        "cleanup",
    ],
)
@pytest.mark.parametrize("with_prefix", [False, True])
def test_native_message_decision_recovers_every_publication_boundary(
    tmp_path, monkeypatch, boundary, with_prefix
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    retained = (
        [message(session.session_id, "msg_retained", "queue_retained")] if with_prefix else []
    )
    if retained:
        append_input_messages(store, retained)
    request = message_request(
        store.load_session_record(session.session_id), [message(session.session_id)]
    )
    directory = store.sessions_dir / session.session_id
    suffix = FileSessionCommits._write_suffix
    connect_data = file_commits.connect_data
    write_index = FileMessageIndex._write
    unlink = Path.unlink
    reached = []

    save_head = file_commits.write_session

    def head_then_fail(connection, session):
        save_head(connection, session)
        if boundary == "head":
            reached.append(boundary)
            raise OSError("injected publication interruption")

    def suffix_then_fail(path, offset, records):
        if boundary == "partial_journal" and path == directory / "messages.jsonl":
            with path.open("r+b") as stream:
                stream.seek(offset)
                stream.write(b'{"message_id":"partial')
                stream.flush()
            reached.append(boundary)
            raise OSError("injected partial journal interruption")
        suffix(path, offset, records)
        if boundary == "journal" and path == directory / "messages.jsonl":
            reached.append(boundary)
            raise OSError("injected journal interruption")

    def write_then_fail(connection, references, counts):
        write_index(connection, references, counts)
        if boundary == "index_uncommitted":
            reached.append(boundary)
            raise OSError("injected index transaction interruption")

    @contextmanager
    def index_then_fail(folder, *, mode="ro", **kwargs):
        with connect_data(folder, mode=mode, **kwargs) as connection:
            yield connection
        if boundary == "index_committed" and mode == "rw":
            reached.append(boundary)
            raise OSError("injected index publication interruption")

    def fail_cleanup(path, *args, **kwargs):
        if boundary == "cleanup" and path == directory / "commit.json":
            reached.append(boundary)
            raise OSError("injected cleanup interruption")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_session", head_then_fail)
        patch.setattr(FileSessionCommits, "_write_suffix", staticmethod(suffix_then_fail))
        patch.setattr(file_commits, "connect_data", index_then_fail)
        patch.setattr(FileMessageIndex, "_write", staticmethod(write_then_fail))
        patch.setattr(Path, "unlink", fail_cleanup)
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(request)
    assert reached == [boundary]
    reader = LocalSessionStore(tmp_path)
    assert_pending_reads(reader, session.session_id, "msg_selected")
    with reader.session_mutation(session.session_id):
        pass
    with pytest.raises(SessionCommitConflict, match="already applied"):
        reader.commit_session(request)
    resolved = reader.load_message(session.session_id, "msg_selected")
    assert reader.load_session_record(session.session_id).last_commit_id == request.operation_id
    assert resolved.text_content == "unique message body"
    assert reader.load_messages_page(session.session_id) == ([*retained, resolved], None)
    assert reader.load_run_messages(session.session_id, "run_selected") == [*retained, resolved]
    assert reader.find_queued_input_message(session.session_id, "queue_selected") == resolved
    assert reader.load_session_record(session.session_id).message_count == len(retained) + 1
    assert not (directory / "commit.json").exists()
    with closing(sqlite3.connect(directory / "session-data.sqlite3")) as connection:
        assert (
            connection.execute("SELECT count(*) FROM locations").fetchone()[0] == len(retained) + 1
        )
    assert not (directory / "message-index").exists()
    assert not (directory / "messages").exists()
    assert [
        json.loads(line)["message_id"]
        for line in (directory / "messages.jsonl").read_text().splitlines()
    ] == [item.message_id for item in [*retained, resolved]]
    assert b"unique message body" not in (directory / "messages.jsonl").read_bytes()


@pytest.mark.parametrize("boundary", ["index_uncommitted", "index_committed", "journal"])
def test_undo_cleanup_survives_a_partial_fact_removal(tmp_path, monkeypatch, boundary):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    messages = [message(session.session_id, f"msg_{i}") for i in range(4)]
    append_input_messages(store, messages)
    directory = store.sessions_dir / session.session_id
    request = SessionCommit(
        before=store.load_session_record(session.session_id),
        before_message_id=messages[2].message_id,
    )
    suffix = FileSessionCommits._write_suffix
    connect_data = file_commits.connect_data
    write_index = FileMessageIndex._write
    reached = []

    def write_then_fail(connection, references, counts):
        write_index(connection, references, counts)
        if boundary == "index_uncommitted":
            reached.append(boundary)
            raise OSError("injected index transaction interruption")

    @contextmanager
    def index_then_fail(folder, *, mode="ro", **kwargs):
        with connect_data(folder, mode=mode, **kwargs) as connection:
            yield connection
        if boundary == "index_committed" and mode == "rw":
            reached.append(boundary)
            raise OSError("injected index cut interruption")

    def suffix_then_fail(path, offset, records):
        suffix(path, offset, records)
        if boundary == "journal" and path == directory / "messages.jsonl":
            reached.append(boundary)
            raise OSError("injected journal cut interruption")

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "connect_data", index_then_fail)
        patch.setattr(FileMessageIndex, "_write", staticmethod(write_then_fail))
        patch.setattr(FileSessionCommits, "_write_suffix", staticmethod(suffix_then_fail))
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(request)
    assert reached == [boundary]
    reader = LocalSessionStore(tmp_path)
    assert_pending_reads(reader, session.session_id, messages[0].message_id)
    with reader.session_mutation(session.session_id):
        pass
    assert reader.load_session_record(session.session_id).message_count == 2
    with pytest.raises(SessionCommitConflict, match="already applied"):
        reader.commit_session(request)
    assert reader.load_run_messages(session.session_id, "run_selected") == messages[:2]
    # One accepted Input can publish multiple messages; the lookup selects its retained tail.
    assert reader.find_queued_input_message(session.session_id, "queue_selected") == messages[1]
    assert reader.load_messages_page(session.session_id) == (messages[:2], None)
    for removed in messages[2:]:
        with pytest.raises(PersistenceError, match="message not found"):
            reader.load_message(session.session_id, removed.message_id)
        with session_connection(reader, session.session_id) as connection:
            assert FileMessageIndex().get(connection, removed.message_id) is None


def test_unaccepted_decision_never_publishes_message_facts(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    request = message_request(session, [message(session.session_id)])
    replace = local_io.replace_path_with_retry

    def fail_decision(source, destination):
        if destination.name == "commit.json":
            raise OSError("injected decision rejection")
        return replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(local_io, "replace_path_with_retry", fail_decision)
        with pytest.raises(PersistenceError, match="injected"):
            store.commit_session(request)
    assert store.load_messages_page(session.session_id) == ([], None)
    assert store.load_run_messages(session.session_id, "run_selected") == []
    assert store.find_queued_input_message(session.session_id, "queue_selected") is None
    with session_connection(store, session.session_id) as connection:
        assert FileMessageIndex().get(connection, "msg_selected") is None
    committed = store.commit_session(request)
    assert store.load_message(
        session.session_id, committed.messages[0].message_id
    ).text_content == ("unique message body")


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_snapshot_run_queries_preserve_order_without_payload_sequences(tmp_path, backend):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    messages = [message(session.session_id, f"msg_{identity}") for identity in [3, 1, 2]]
    assert all(item.sequence == 0 for item in messages)
    store.create_session_snapshot(session, messages, **input_snapshot(session, messages))
    expected = [
        item.model_copy(update={"sequence": index}) for index, item in enumerate(messages, 1)
    ]
    assert store.load_run_messages(session.session_id, "run_selected") == expected
    assert store.find_queued_input_message(session.session_id, "queue_selected") == expected[-1]


def test_duplicate_message_identity_does_not_replace_the_fact(tmp_path):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    original = message(session.session_id)
    append_input_messages(store, [original])
    changed = MessageRecord.text(
        session_id=session.session_id, role="user", text="changed"
    ).model_copy(update={"message_id": original.message_id})
    with pytest.raises(SessionCommitConflict, match="already committed"):
        store.append_message(changed)
    assert store.load_message(session.session_id, original.message_id) == original


def test_append_does_not_decode_or_index_existing_message_bodies(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    messages = [message(session.session_id, f"msg_{i}", "queue_old") for i in range(30)]
    store.create_session_snapshot(session, messages, **input_snapshot(session, messages))
    original = Path.open

    def guarded(path, mode="r", *args, **kwargs):
        if path.name == "messages.jsonl" and mode in {"r", "rb"}:
            pytest.fail("append indexed historical order records")
        return original(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded)
        reader = LocalSessionStore(tmp_path)
        append_input_messages(reader, [message(session.session_id)])
    assert store.load_messages_tail(session.session_id, limit=1)[0].message_id == "msg_selected"


@pytest.mark.parametrize("field", ["message_id", "session_id"])
def test_point_message_read_rejects_a_foreign_fact(tmp_path, field):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    original = message(session.session_id)
    store.create_session_snapshot(session, [original], **input_snapshot(session, [original]))
    path = store.sessions_dir / session.session_id / "messages.jsonl"
    payload = json.loads(path.read_text())
    payload[field] = "foreign"
    path.write_text(json.dumps(payload))
    with pytest.raises(PersistenceError, match="foreign|another Session"):
        store.load_message(session.session_id, original.message_id)
