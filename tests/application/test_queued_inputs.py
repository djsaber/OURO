from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from ouro.application.queued_inputs import QUEUED_INPUTS_KEY, QueuedInputQueue
from ouro.core.messages import MessageRecord, SkillActivationContent
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import HostSessionInput, SessionInputRequest
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def test_queued_input_queue_persists_items_fifo_and_clears_when_empty(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Queued inputs")
    store.create_session(session)
    store.save_run(RunRecord(session_id=session.session_id, status="running"))
    queue = QueuedInputQueue(store)

    snapshot = store.load_session(session.session_id)
    session, _, _ = queue.enqueue(
        snapshot=snapshot,
        prompt="first",
        title=None,
        role="user",
        scaffold=False,
    )
    snapshot = store.load_session(session.session_id)
    session, _, size = queue.enqueue(
        snapshot=snapshot,
        prompt="second",
        title="Second",
        role="user",
        scaffold=False,
    )

    snapshot = store.load_session(session.session_id)
    assert size == 2
    assert snapshot.session.metadata[QUEUED_INPUTS_KEY][0]["prompt"] == "first"
    assert snapshot.session.metadata[QUEUED_INPUTS_KEY][1]["title"] == "Second"

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
    assert QUEUED_INPUTS_KEY not in snapshot.session.metadata


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

    _, first, _, _ = queue.enqueue_batch(
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
    _, replacement, _, superseded = queue.enqueue_batch(
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


def test_queued_input_queue_removes_invalid_item(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid input", metadata={QUEUED_INPUTS_KEY: [{}]})
    store.create_session(session)
    queue = QueuedInputQueue(store)

    item = queue.peek(store.load_session(session.session_id))
    snapshot = store.load_session(session.session_id)

    assert item is None
    assert QUEUED_INPUTS_KEY not in snapshot.session.metadata


def test_queued_input_queue_drops_host_item_without_source_attribution(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(
        title="Invalid Host input",
        metadata={
            QUEUED_INPUTS_KEY: [
                {
                    "prompt": "anonymous framework input",
                    "mode": "follow_up",
                    "source": "host",
                }
            ]
        },
    )
    store.create_session(session)
    queue = QueuedInputQueue(store)

    item = queue.peek(store.load_session(session.session_id))
    snapshot = store.load_session(session.session_id)

    assert item is None
    assert QUEUED_INPUTS_KEY not in snapshot.session.metadata


def test_queued_input_queue_read_does_not_consume(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    queued_session, _, _ = queue.enqueue(
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
    _, user_input, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="Return this draft",
        title=None,
        role="user",
        scaffold=False,
    )
    _, host_input, _ = queue.enqueue(
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
    assert cancelled == user_input
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

    queued_session, queued, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="",
        skill_activation=activation,
        title=None,
        role="user",
        scaffold=False,
    )
    restored = queue.peek(store.load_session(queued_session.session_id))

    assert queued.skill_activation == activation
    assert restored is not None
    assert restored.skill_activation == activation
    with pytest.raises(ValidationError, match="cannot steer"):
        queue.enqueue(
            snapshot=store.load_session(session.session_id),
            prompt="task",
            skill_activation=activation,
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
    _, queued, _ = queue.enqueue(
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

    queued_session, queued, _ = queue.enqueue(
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
    _, host_user_message, _ = queue.enqueue(
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
    session = SessionRecord(
        title="Incomplete plugin queue",
        metadata={
            QUEUED_INPUTS_KEY: [
                {
                    "prompt": "incomplete plugin continuation",
                    "mode": "follow_up",
                    "source": "plugin",
                    "plugin_id": "dev.example.plugin",
                    "contribution_id": "continuation",
                    "generation_id": "generation_1",
                    "execution_contract_hash": "contract_1",
                    "semantic_tag": "plugin-message",
                }
            ]
        },
    )
    store.create_session(session)
    restored = QueuedInputQueue(store).peek(store.load_session(session.session_id))

    assert restored is None
    assert QUEUED_INPUTS_KEY not in store.load_session_record(session.session_id).metadata


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

    queued_session, queued, _ = queue.enqueue(
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
    prompts = [item["prompt"] for item in reloaded.session.metadata[QUEUED_INPUTS_KEY]]
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
    with ThreadPoolExecutor(max_workers=4) as executor:
        accepted = list(
            executor.map(
                lambda i: queues[i % 2].accept_host(session.session_id, request),
                range(12),
            )
        )
    assert sum(item is not None for _, item, _ in accepted) == 1
    assert len(queues[0].eligible_session(first.load_session_record(session.session_id))) == 1
    with pytest.raises(ValueError, match="already bound"):
        queues[1].accept_host(session.session_id, request.model_copy(update={"prompt": "changed"}))
    message = MessageRecord.text(
        session_id=session.session_id,
        run_id="run_result",
        role=request.role,
        scaffold=request.scaffold,
        semantic_tag=request.semantic_tag,
        text=request.prompt,
    ).model_copy(
        update={
            "metadata": {
                "queued_input_id": request.queue_id,
                "queued_input_mode": request.mode,
                "host_source_id": request.host_source_id,
            }
        }
    )
    with first.session_mutation(session.session_id):
        before = first.load_session_record(session.session_id)
        first.commit_session(
            SessionCommit(
                before=before,
                after=queues[0].without_inputs(before, (request.queue_id,)),
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
        receipt.message_id == message.message_id and item is None for receipt, item, _ in retried
    )
    assert queues[0].eligible_session(first.load_session_record(session.session_id)) == []
    with pytest.raises(ValueError, match="already bound"):
        queues[1].accept_host(session.session_id, request.model_copy(update={"mode": "steer"}))
