import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest
from pydantic import ValidationError

from ouro import local_io
from ouro.application.inputs.queue import QueuedInputQueue, consumption_changes
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import InputMessageReference, MessageRecord, SkillActivationContent
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import HostSessionInput, SessionInputRequest
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files.inputs import input_path


def test_queued_input_queue_persists_items_fifo_and_clears_when_empty(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Queued inputs")
    store.create_session(session)
    store.save_run(RunRecord(session_id=session.session_id, status="running"))
    queue = QueuedInputQueue(store)

    snapshot = store.load_session(session.session_id)
    session, _, _, _ = queue.enqueue(
        snapshot=snapshot,
        prompt="first",
        title=None,
        role="user",
        scaffold=False,
    )
    snapshot = store.load_session(session.session_id)
    session, _, size, _ = queue.enqueue(
        snapshot=snapshot,
        prompt="second",
        title="Second",
        role="user",
        scaffold=False,
    )

    snapshot = store.load_session(session.session_id)
    assert size == 2
    pending = queue.eligible(snapshot)
    assert pending[0].prompt == "first"
    assert pending[1].title == "Second"
    assert "follow_up_queue" not in snapshot.session.metadata

    first = queue.peek(snapshot)
    assert first is not None
    queue.cancel_user_input(session_id=session.session_id, queue_id=first.queue_id)
    snapshot = store.load_session(session.session_id)
    second = queue.peek(snapshot)
    assert second is not None
    queue.cancel_user_input(session_id=session.session_id, queue_id=second.queue_id)
    snapshot = store.load_session(session.session_id)

    assert first.prompt == "first"
    assert first.title is None
    assert second.prompt == "second"
    assert second.title == "Second"
    assert snapshot.session.pending_input_ids == ()


def test_atomic_batch_is_one_ordered_unit_and_coalesces_only_its_owner(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Atomic batches")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    attribution = {
        "source": "plugin",
        "plugin_id": "dev.example.goal",
        "contribution_id": "goal",
        "generation_id": "generation_1",
        "execution_contract_hash": "contract_1",
    }

    _, first, _, _, _ = queue.enqueue_batch(
        snapshot=store.load_session(session.session_id),
        inputs=(
            SessionInputRequest(
                prompt="control",
                role="user",
                semantic_tag="goal-updated",
            ),
            SessionInputRequest(prompt="objective", role="user"),
        ),
        coalesce_key="goal-lifecycle",
        **attribution,
    )
    queue.enqueue_batch(
        snapshot=store.load_session(session.session_id),
        inputs=(SessionInputRequest(prompt="another owner"),),
        source="plugin",
        plugin_id="dev.example.other",
        contribution_id="goal",
        generation_id="generation_1",
        execution_contract_hash="contract_1",
        coalesce_key="goal-lifecycle",
    )
    _, replacement, _, superseded, _ = queue.enqueue_batch(
        snapshot=store.load_session(session.session_id),
        inputs=(SessionInputRequest(prompt="final control", role="user"),),
        coalesce_key="goal-lifecycle",
        **attribution,
    )

    units = queue.eligible_units(store.load_session(session.session_id))
    assert [item.prompt for item in superseded] == ["control", "objective"]
    assert len({item.batch_id for item in first}) == 1
    assert [item.batch_index for item in first] == [0, 1]
    assert [[item.prompt for item in unit] for unit in units] == [
        ["another owner"],
        ["final control"],
    ]
    assert units[-1] == replacement


def test_queued_input_query_reports_missing_state_without_repairing(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid input")
    store.create_session(session)
    queue = QueuedInputQueue(store)

    session, item, _, _ = queue.enqueue(
        snapshot=store.load_runtime_session(session.session_id),
        prompt="accepted",
        title=None,
        role="user",
        scaffold=False,
    )
    path = input_path(store.sessions_dir / session.session_id, item.queue_id, state=True)
    path.unlink()
    with pytest.raises(PersistenceError, match="state not found"):
        queue.peek(store.load_runtime_session(session.session_id))
    assert store.load_session_record(session.session_id).pending_input_ids == (item.queue_id,)
    assert not path.exists()


def test_queued_input_queue_rejects_host_item_without_source_attribution(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid Host input")
    store.create_session(session)
    queue = QueuedInputQueue(store)

    with pytest.raises(ValidationError, match="requires a source id"):
        queue.enqueue(
            snapshot=store.load_runtime_session(session.session_id),
            prompt="anonymous",
            title=None,
            role="user",
            scaffold=False,
            source="host",
        )
    assert store.load_session_record(session.session_id).pending_input_ids == ()


def test_queued_input_queue_read_does_not_consume(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    queued_session, _, _, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="second",
        title=None,
        role="user",
        scaffold=False,
    )
    snapshot = store.load_session(queued_session.session_id)

    first = queue.peek(snapshot)
    repeated = queue.peek(snapshot)

    assert first is not None
    assert repeated is not None
    assert repeated.queue_id == first.queue_id
    queue.cancel_user_input(session_id=session.session_id, queue_id=first.queue_id)
    assert queue.peek(store.load_session(session.session_id)) is None


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_queue_cancels_only_the_selected_direct_user_input(tmp_path, backend: str) -> None:
    store = (
        LocalSessionStore(tmp_path / "sessions")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "ouro.db")
    )
    store.initialize()
    session = SessionRecord(title="Cancelable queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    _, user_input, _, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="Return this draft",
        title=None,
        role="user",
        scaffold=False,
    )
    _, host_input, _, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="Framework continuation",
        title=None,
        source="host",
        role="system",
        scaffold=True,
        host_source_id="ouro.runtime/test",
    )

    cancelled = queue.cancel_user_input(
        session_id=session.session_id,
        queue_id=user_input.queue_id,
    )

    remaining = queue.eligible_session(store.load_session_record(session.session_id))
    assert cancelled is not None and cancelled[1] == user_input
    assert (
        queue.cancel_user_input(
            session_id=session.session_id,
            queue_id=user_input.queue_id,
        )
        is None
    )
    assert (
        queue.cancel_user_input(
            session_id=session.session_id,
            queue_id=host_input.queue_id,
        )
        is None
    )
    assert remaining == [host_input]


def test_queue_persists_argumentless_user_skill_activation_and_rejects_steering(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Skill queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    activation = SkillActivationContent(
        name="reviewer",
        source="workspace",
        location=str(tmp_path / "skills" / "reviewer" / "SKILL.md"),
        base_directory=str(tmp_path / "skills" / "reviewer"),
        instructions="Review carefully.",
        content_digest=f"sha256:{'a' * 64}",
    )

    queued_session, queued, _, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="",
        skill_activations=(activation,),
        title=None,
        role="user",
        scaffold=False,
    )
    restored = queue.peek(store.load_session(queued_session.session_id))

    assert queued.skill_activations == (activation,)
    assert restored is not None
    assert restored.skill_activations == (activation,)
    with pytest.raises(ValidationError, match="cannot steer"):
        queue.enqueue(
            snapshot=store.load_session(session.session_id),
            prompt="task",
            skill_activations=(activation,),
            title=None,
            mode="steer",
            role="user",
            scaffold=False,
        )


def test_host_queued_input_requires_and_preserves_source_attribution(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Host queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    snapshot = store.load_session(session.session_id)

    with pytest.raises(ValidationError, match="Host queued input requires a source id"):
        queue.enqueue(
            snapshot=snapshot,
            prompt="anonymous framework input",
            title=None,
            source="host",
            role="system",
            scaffold=True,
        )
    _, queued, _, _ = queue.enqueue(
        snapshot=snapshot,
        prompt="recover from checkpoint",
        title="Recovery",
        source="host",
        role="system",
        scaffold=True,
        host_source_id="ouro.runtime/recovery",
        resource_refs=("resource_1",),
    )
    restored = queue.peek(store.load_session(session.session_id))

    assert queued.source == "host"
    assert restored is not None
    assert restored.source == "host"
    assert restored.host_source_id == "ouro.runtime/recovery"
    assert restored.resource_refs == ("resource_1",)


def test_plugin_queue_preserves_message_role_independently_from_provenance(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Plugin user-role queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    snapshot = store.load_session(session.session_id)

    queued_session, queued, _, _ = queue.enqueue(
        snapshot=snapshot,
        prompt="The user's explicit command intent",
        title=None,
        source="plugin",
        role="user",
        scaffold=True,
        semantic_tag="plugin-intent",
        plugin_id="dev.example.plugin",
        contribution_id="command",
        generation_id="generation_1",
        execution_contract_hash="contract_1",
    )
    restored = queue.peek(store.load_session(queued_session.session_id))

    assert queued.source == "plugin"
    assert queued.role == "user"
    assert queued.scaffold is True
    assert queued.semantic_tag == "plugin-intent"
    assert restored is not None
    assert restored.source == "plugin"
    assert restored.role == "user"
    assert restored.scaffold is True
    assert restored.semantic_tag == "plugin-intent"
    assert restored.plugin_id == "dev.example.plugin"
    _, host_user_message, _, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="Host-owned user-role scaffold",
        title=None,
        source="host",
        role="user",
        scaffold=True,
        host_source_id="ouro.runtime/test",
    )
    assert host_user_message.source == "host"
    assert host_user_message.role == "user"
    assert host_user_message.scaffold is True


def test_queue_rejects_persisted_items_missing_explicit_message_semantics(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Incomplete plugin queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    _, item, _, _ = queue.enqueue(
        snapshot=store.load_runtime_session(session.session_id),
        prompt="plugin",
        title=None,
        role="user",
        scaffold=True,
        source="plugin",
        plugin_id="dev.example.plugin",
        contribution_id="continuation",
        generation_id="generation_1",
        execution_contract_hash="contract_1",
    )
    path = input_path(store.sessions_dir / session.session_id, item.queue_id)
    payload = json.loads(path.read_text())
    del payload["item"]["role"]
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(PersistenceError, match="could not validate"):
        queue.peek(store.load_runtime_session(session.session_id))
    assert path.read_bytes() == before


def test_delegated_task_input_requires_and_preserves_delegation_identity(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Delegated task queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    snapshot = store.load_session(session.session_id)

    with pytest.raises(ValidationError, match="requires a delegation id"):
        queue.enqueue(
            snapshot=snapshot,
            prompt="Inspect the target.",
            title=None,
            source="delegation",
            role="user",
            scaffold=True,
        )

    queued_session, queued, _, _ = queue.enqueue(
        snapshot=snapshot,
        prompt="Inspect the target.",
        title=None,
        source="delegation",
        role="user",
        scaffold=True,
        delegation_id="dlg_task",
    )
    restored = queue.peek(store.load_session(queued_session.session_id))

    assert queued.delegation_id == "dlg_task"
    assert restored is not None
    assert restored.source == "delegation"
    assert restored.delegation_id == "dlg_task"


def test_queued_input_queue_merges_concurrent_stale_snapshot_enqueues(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Concurrent queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    stale = store.load_session(session.session_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                queue.enqueue,
                snapshot=stale,
                prompt=f"prompt-{index}",
                title=None,
                role="user",
                scaffold=False,
            )
            for index in range(20)
        ]
        for future in futures:
            future.result(timeout=2)

    reloaded = store.load_session(session.session_id)
    prompts = [item.prompt for item in queue.eligible(reloaded)]
    assert sorted(prompts) == sorted(f"prompt-{index}" for index in range(20))


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_host_receipt_is_atomic_across_pending_and_consumed_input(tmp_path, backend):
    def repository():
        return (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.db")
        )

    first, second = repository(), repository()
    first.initialize()
    second.initialize()
    session = SessionRecord()
    first.create_session(session)
    queues = [QueuedInputQueue(first), QueuedInputQueue(second)]
    request = HostSessionInput(prompt="result", host_source_id="test/result", queue_id="delivery")
    selecting = Barrier(4)

    def select_driver(identity):
        assert identity is None
        selecting.wait(timeout=5)
        return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        accepted = list(
            executor.map(
                lambda i: queues[i % 2].accept_host(
                    session.session_id, request, select_driver=select_driver
                ),
                range(12),
            )
        )
    assert sum(item is not None for _, item, _, _ in accepted) == 1
    assert len(queues[0].eligible_session(first.load_session_record(session.session_id))) == 1
    for change in ({"prompt": "changed"}, {"title": "changed"}, {"history_epoch": 0}):
        with pytest.raises(ValueError, match="already bound"):
            queues[1].accept_host(session.session_id, request.model_copy(update=change))
    message = MessageRecord.text(
        session_id=session.session_id,
        run_id="run_result",
        role=request.role,
        scaffold=request.scaffold,
        semantic_tag=request.semantic_tag,
        text=request.prompt,
    ).model_copy(
        update={
            "input_ref": InputMessageReference(input_id=request.queue_id),
            "metadata": {
                "queued_input_mode": request.mode,
                "host_source_id": request.host_source_id,
            },
        }
    )
    with first.session_mutation(session.session_id):
        before = first.load_session_record(session.session_id)
        first.commit_session(
            SessionCommit(
                before=before,
                input_changes=consumption_changes((request.queue_id,), [message]),
                steps=(SessionCommitStep(message=message),),
            )
        )
    with ThreadPoolExecutor(max_workers=4) as executor:
        retried = list(
            executor.map(
                lambda i: queues[i % 2].accept_host(session.session_id, request),
                range(12),
            )
        )
    assert all(
        receipt.message_id == message.message_id and item is None for receipt, item, _, _ in retried
    )
    assert queues[0].eligible_session(first.load_session_record(session.session_id)) == []
    with pytest.raises(ValueError, match="already bound"):
        queues[1].accept_host(session.session_id, request.model_copy(update={"mode": "steer"}))


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("operation", ["accept", "cancel"])
def test_queue_state_and_event_share_native_failure_boundary(
    tmp_path, monkeypatch, backend, operation
):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    queue = QueuedInputQueue(store)
    snapshot = store.load_runtime_session(session.session_id)

    def accept():
        return queue.enqueue(
            snapshot=snapshot,
            prompt="accepted once",
            title=None,
            role="user",
            scaffold=False,
            queue_id="input-atomic",
        )

    if operation == "cancel":
        accept()

    def change():
        if operation == "accept":
            return accept()
        return queue.cancel_user_input(session_id=session.session_id, queue_id="input-atomic")

    if backend == "files":
        target = input_path(store.sessions_dir / session.session_id, "input-atomic", state=True)
        replace = local_io.replace_path_with_retry
        interrupted = []

        def interrupt(source, destination):
            replace(source, destination)
            if destination == target:
                interrupted.append(True)
                raise OSError("input state installed before event")

        with monkeypatch.context() as patch:
            patch.setattr(local_io, "replace_path_with_retry", interrupt)
            with pytest.raises(PersistenceError):
                change()
        assert interrupted == [True]
        with pytest.raises(SessionRecoveryRequiredError):
            store.load_session_record(session.session_id)
        with store.session_mutation(session.session_id):
            pass
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_input_event BEFORE INSERT ON events BEGIN "
                "SELECT RAISE(ABORT, 'input event'); END"
            )
        with pytest.raises(PersistenceError):
            change()
        assert store.load_session_record(session.session_id).pending_input_ids == (
            ("input-atomic",) if operation == "cancel" else ()
        )
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_input_event")
        change()

    before = store.load_session(session.session_id)
    if operation == "accept":
        assert accept()[-1] == ()
    else:
        assert change() is None
    after = store.load_session(session.session_id)
    assert after.events == before.events
    types = [event.event_type for event in after.events]
    assert types == ["follow_up_input_queued"] + (
        ["queued_input_cancelled"] if operation == "cancel" else []
    )
    assert after.session.pending_input_ids == (("input-atomic",) if operation == "accept" else ())
    assert store.load_input(session.session_id, "input-atomic").item.prompt == "accepted once"
