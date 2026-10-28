from contextlib import closing

import pytest

import ouro.sessions.backends.files.commits as file_commits
from ouro.core.compaction import CompactionRecord
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.state import SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files.journal import reverse_jsonl
from tests.tool_batches import seed_tool_messages


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "history.sqlite")
    )
    result.initialize()
    return result


def history(store):
    session = SessionRecord()
    messages = [
        MessageRecord.text(
            session_id=session.session_id, role=role, text=str(index), sequence=index + 1
        )
        for index, role in enumerate(["user", "assistant", "user", "assistant"])
    ]
    session = session.model_copy(
        update={"message_count": 4, "last_message_id": messages[-1].message_id}
    )
    store.create_session(session)
    store.append_messages(messages)
    return session, messages


def test_cut_validates_head_and_boundary_before_mutating(store):
    session, messages = history(store)
    for boundary, head in [(messages[2].message_id, "stale"), ("unknown", messages[-1].message_id)]:
        with pytest.raises(PersistenceError):
            store.truncate_messages(
                session.session_id, before_message_id=boundary, expected_last_message_id=head
            )
        assert store.load_session(session.session_id).messages == messages
    result = store.truncate_messages(
        session.session_id,
        before_message_id=messages[2].message_id,
        expected_last_message_id=messages[-1].message_id,
    )
    assert result.first_removed == messages[2].as_fact()
    assert result.last_retained == messages[1].as_fact()
    assert result.removed_count == 2
    assert store.load_messages_tail(session.session_id) == list(reversed(messages[:2]))
    store.append_message(messages[2])
    assert store.load_messages_tail(session.session_id, limit=1) == [messages[2]]


def test_cut_preserves_the_exact_prefix_even_inside_a_tool_exchange(store):
    session = SessionRecord(message_count=4)
    store.create_session(session)
    calls = [ToolCallRequest(id=f"call_{index}", name="Read", arguments={}) for index in range(2)]
    assistant = MessageRecord.text(
        session_id=session.session_id,
        role="assistant",
        text="",
        sequence=1,
        run_id="run_tools",
        turn_id="turn_tools",
    ).model_copy(update={"tool_calls": calls})
    results = [
        MessageRecord.text(
            session_id=session.session_id,
            role="tool_result",
            text="ok",
            sequence=index + 2,
            run_id="run_tools",
            turn_id="turn_tools",
        ).model_copy(
            update={"tool_result": ToolResultReference(tool_call_id=call.id, tool_name=call.name)}
        )
        for index, call in enumerate(calls)
    ]
    user = MessageRecord.text(session_id=session.session_id, role="user", text="next", sequence=4)
    seed_tool_messages(store, assistant, results)
    store.append_messages([assistant, *results, user])
    store.truncate_messages(
        session.session_id,
        before_message_id=results[1].message_id,
        expected_last_message_id=user.message_id,
    )
    assert store.load_messages_tail(session.session_id) == [results[0], assistant]

    broken = SessionRecord()
    store.create_session(broken)
    partial = [
        message.model_copy(
            update={"session_id": broken.session_id, "message_id": f"broken_{index}"}
        )
        for index, message in enumerate([assistant, results[0], user])
    ]
    seed_tool_messages(store, partial[0], [partial[1]])
    store.append_messages(partial)
    store.truncate_messages(
        broken.session_id,
        before_message_id=partial[-1].message_id,
        expected_last_message_id=partial[-1].message_id,
    )
    assert store.load_messages_tail(broken.session_id) == list(reversed(partial[:-1]))


def test_cut_withdraws_incomplete_suffix_without_requiring_complete_retained_text(store):
    session = SessionRecord(message_count=4)
    store.create_session(session)
    messages = [
        MessageRecord.text(
            session_id=session.session_id, role=role, text=text, sequence=index + 1
        ).model_copy(update={"metadata": {"incomplete": True} if role == "assistant" else {}})
        for index, (role, text) in enumerate(
            [
                ("user", "task"),
                ("assistant", "older partial"),
                ("user", "continue"),
                ("assistant", "new partial"),
            ]
        )
    ]
    store.append_messages(messages)
    result = store.truncate_messages(
        session.session_id,
        before_message_id=messages[2].message_id,
        expected_last_message_id=messages[-1].message_id,
    )
    assert result.removed_count == 2
    assert store.load_messages_tail(session.session_id) == list(reversed(messages[:2]))


@pytest.mark.parametrize("cut_index,active", [(2, True), (0, False)])
def test_cut_only_invalidates_compaction_when_coverage_is_removed(store, cut_index, active):
    session, messages = history(store)
    compaction = CompactionRecord(
        session_id=session.session_id,
        strategy_id="test",
        trigger="manual",
        summary="older context",
        covered_message_count=2,
        covered_end_message_id=messages[1].message_id,
        history_boundary_message_id=messages[1].message_id,
    )
    from tests.session_summaries import commit_summary

    commit_summary(store, compaction)
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[cut_index].message_id,
        expected_last_message_id=messages[-1].message_id,
    )
    assert (
        store.load_session_record(session.session_id).context_selection.compaction_id is not None
    ) == active


def test_cut_preserves_valid_compaction_chain_and_allows_new_compaction(store):
    session, messages = history(store)
    previous = CompactionRecord(
        session_id=session.session_id,
        strategy_id="test",
        trigger="manual",
        summary="retained context",
        covered_message_count=1,
        covered_end_message_id=messages[0].message_id,
        history_boundary_message_id=messages[1].message_id,
    )
    removed = previous.model_copy(
        update={
            "compaction_id": "removed_summary",
            "previous_compaction_id": previous.compaction_id,
            "history_boundary_message_id": messages[3].message_id,
            "covered_message_count": 4,
            "covered_end_message_id": messages[3].message_id,
        }
    )
    for compaction in (previous, removed):
        from tests.session_summaries import commit_summary

        commit_summary(store, compaction)
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[2].message_id,
        expected_last_message_id=messages[-1].message_id,
    )
    assert store.load_compactions(session.session_id) == [previous]
    assert (
        store.load_session_record(session.session_id).context_selection.compaction_id
        == previous.compaction_id
    )
    following = previous.model_copy(
        update={
            "compaction_id": "following_summary",
            "previous_compaction_id": previous.compaction_id,
        }
    )
    commit_summary(store, following)
    assert store.load_compactions(session.session_id) == [previous, following]


def test_local_cut_recovers_after_failure_between_truncation_and_projection(tmp_path, monkeypatch):

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session, messages = history(store)

    def fail_session_write(directory, model):
        raise OSError("injected projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_session", fail_session_write)
        with pytest.raises(PersistenceError, match="injected"):
            store.truncate_messages(
                session.session_id,
                before_message_id=messages[2].message_id,
                expected_last_message_id=messages[-1].message_id,
            )
    reopened = LocalSessionStore(tmp_path)
    with pytest.raises(SessionRecoveryRequiredError):
        reopened.load_session(session.session_id)
    with reopened.session_mutation(session.session_id):
        pass
    restored = reopened.load_session(session.session_id)
    assert restored.messages == messages[:2]
    assert restored.session.last_message_id == messages[1].message_id
    assert restored.session.message_count == 2
    assert not (store.sessions_dir / session.session_id / "commit.json").exists()


def test_sqlite_cut_rolls_back_if_projection_fails(tmp_path, monkeypatch):
    from ouro.sessions.backends.sqlite.commits import SQLiteSessionCommits

    store = SQLiteSessionStore(tmp_path / "rollback.sqlite")
    store.initialize()
    session, messages = history(store)

    commit = SQLiteSessionCommits.commit

    def fail_save(commits, request):
        commit(commits, request)
        raise RuntimeError("injected failure")

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteSessionCommits, "commit", fail_save)
        with pytest.raises(RuntimeError, match="injected"):
            store.truncate_messages(
                session.session_id,
                before_message_id=messages[2].message_id,
                expected_last_message_id=messages[-1].message_id,
            )
    assert store.load_session(session.session_id).messages == messages


def test_local_cut_reads_only_suffix_and_never_rewrites_prefix(tmp_path, monkeypatch):
    import ouro.sessions.backends.files.messages as module

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user" if index % 2 == 0 else "assistant",
            text="x" * 2_000_000 if index == 0 else str(index),
        )
        for index in range(4)
    ]
    store.append_messages(messages)
    directory = store.sessions_dir / session.session_id
    path = directory / "messages.jsonl"
    prefix_bytes = path.read_bytes().splitlines(keepends=True)[0]
    reads = []
    original = module.decode_message_content

    def counted(payload, **kwargs):
        reads.append(len(payload))
        return original(payload, **kwargs)

    monkeypatch.setattr(module, "decode_message_content", counted)
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[2].message_id,
        expected_last_message_id=messages[-1].message_id,
    )
    # Only the first removed and last retained boundary facts are decoded.
    assert len(reads) == 2
    assert sum(reads) < 10000
    assert path.read_bytes().startswith(prefix_bytes)


@pytest.mark.parametrize("block_size", [1, 7, 64, 65536])
def test_reverse_journal_preserves_utf8_crlf_offsets_and_large_records(tmp_path, block_size):
    path = tmp_path / "messages.jsonl"
    lines = [b"first", ("中文🙂" * 100).encode(), b"last"]
    payload = b"\r\n".join(lines) + b"\r\n"
    path.write_bytes(payload)
    with closing(reverse_jsonl(path, block_size=block_size)) as records:
        found = list(records)
    assert [line for _, line in found] == list(reversed(lines))
    assert all(payload[offset:].startswith(line) for offset, line in found)
