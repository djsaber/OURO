from __future__ import annotations

import pytest

from ouro.core.messages import MessageRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.db" if request.param is SQLiteSessionStore else tmp_path
    )
    result.initialize()
    return result


def message(session_id, run_id, text, *, turn_id=None, delivery=None):
    return MessageRecord.text(
        session_id=session_id,
        run_id=run_id,
        role="assistant" if turn_id else "user",
        text=text,
        turn_id=turn_id,
    ).model_copy(
        update={"metadata": {"context_provider_delivery_key": delivery} if delivery else {}}
    )


def test_run_lookups_preserve_scope_latest_value_and_withdrawal(store):
    session = SessionRecord()
    values = [
        message(session.session_id, "run_a", "first state", delivery="shared/key"),
        message(session.session_id, "run_a", "answer A", turn_id="shared/turn"),
        message(session.session_id, "run_a", "new state", delivery="shared/key"),
        message(session.session_id, "run_b", "answer B", turn_id="shared/turn"),
        message(session.session_id, "run_b", "foreign state", delivery="shared/key"),
    ]
    store.create_session_snapshot(session, values[:2])
    store.append_messages(values[2:])
    assert (
        store.find_context_provider_message(session.session_id, "run_a", "shared/key").text_content
        == "new state"
    )
    assert (
        store.find_context_provider_message(session.session_id, "run_b", "shared/key").text_content
        == "foreign state"
    )
    assert (
        store.find_assistant_message(session.session_id, "run_a", "shared/turn").text_content
        == "answer A"
    )
    assert (
        store.find_assistant_message(session.session_id, "run_b", "shared/turn").text_content
        == "answer B"
    )
    assert store.find_assistant_message(session.session_id, "run_missing", "shared/turn") is None
    assert (
        store.find_last_assistant_message(session.session_id, "run_a").message_id
        == values[1].message_id
    )
    assert (
        store.find_last_assistant_message(session.session_id, "run_b").message_id
        == values[3].message_id
    )
    store.truncate_messages(
        session.session_id,
        before_message_id=values[2].message_id,
        expected_last_message_id=values[-1].message_id,
    )
    assert (
        store.find_context_provider_message(session.session_id, "run_a", "shared/key").text_content
        == "first state"
    )
    assert store.find_context_provider_message(session.session_id, "run_b", "shared/key") is None
    assert store.find_assistant_message(session.session_id, "run_b", "shared/turn") is None
    assert store.find_last_assistant_message(session.session_id, "run_b") is None
    assert (
        store.find_assistant_message(session.session_id, "run_a", "shared/turn").text_content
        == "answer A"
    )


@pytest.mark.parametrize("history_size", [32, 2048])
def test_run_lookup_work_does_not_expand_unrelated_message_bodies(store, monkeypatch, history_size):
    session = SessionRecord()
    history = [
        message(session.session_id, "run_a", "x" * 64, turn_id=f"turn_{index}")
        for index in range(history_size)
    ]
    target = message(session.session_id, "run_a", "selected context", delivery="delivery")
    store.create_session_snapshot(session, [target, *history])
    if isinstance(store, LocalSessionStore):
        reads = []
        entry = store._messages.entry

        def counted(payload, *, session_id):
            selected = entry(payload, session_id=session_id)
            reads.append(selected.message_id)
            return selected

        monkeypatch.setattr(store._messages, "entry", counted)
        found = store.find_context_provider_message(session.session_id, "run_a", "delivery")
        assistant = store.find_assistant_message(session.session_id, "run_a", "turn_0")
        assert reads == [target.message_id, history[0].message_id]
    else:
        instructions = 0

        def progress():
            nonlocal instructions
            instructions += 1
            return 0

        with store.session_read(session.session_id) as connection:
            connection.set_progress_handler(progress, 1)
            try:
                found = store.find_context_provider_message(session.session_id, "run_a", "delivery")
                assistant = store.find_assistant_message(session.session_id, "run_a", "turn_0")
            finally:
                connection.set_progress_handler(None, 0)
        # Count actual VM work, independent of machine speed. A missing composite
        # index scans thousands of JSON payloads in the larger case.
        # Session/connection checks are included (536 instructions before the
        # assistant predicate was simplified); retain room for bounded overhead.
        assert instructions < 1000
    assert found is not None and found.message_id == target.message_id
    assert assistant is not None and assistant.message_id == history[0].message_id


@pytest.mark.parametrize("history_size", [32, 2048])
@pytest.mark.parametrize("input_role", [None, "user", "system"])
def test_result_and_input_membership_read_only_selected_metadata(
    store, monkeypatch, history_size, input_role
):
    session = SessionRecord()
    history = [
        message(session.session_id, "run_a", f"answer {index}", turn_id=f"turn_{index}")
        for index in range(history_size)
    ]
    if input_role:
        history.insert(
            0, MessageRecord.text(session_id=session.session_id, role=input_role, text="input")
        )
    store.create_session_snapshot(session, history)

    def query():
        assert store.has_input_messages(session.session_id) is bool(input_role)
        assert store.has_run_messages(session.session_id, "run_a")
        assert not store.has_run_messages(session.session_id, "run_missing")
        assert store.find_last_assistant_message(session.session_id, "run_missing") is None
        assert (
            store.find_last_assistant_message(session.session_id, "run_a").message_id
            == history[-1].message_id
        )

    if isinstance(store, LocalSessionStore):
        reads = []
        entry = store._messages.entry

        def counted(payload, *, session_id):
            selected = entry(payload, session_id=session_id)
            reads.append(selected.message_id)
            return selected

        monkeypatch.setattr(store._messages, "entry", counted)
        query()
        assert reads == [history[-1].message_id]
    else:
        instructions = 0

        def progress():
            nonlocal instructions
            instructions += 1
            return 0

        with store.session_read(session.session_id) as connection:
            connection.set_progress_handler(progress, 1)
            try:
                query()
            finally:
                connection.set_progress_handler(None, 0)
        # Fixed native work allowance includes Session checks and one response read.
        assert instructions < 1000
