from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from ouro.application.queued_inputs import QUEUED_INPUTS_KEY, QueuedInputQueue
from ouro.core.messages import SkillActivationContent
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
    session, _, _ = queue.enqueue(snapshot=snapshot, prompt="first", title=None)
    snapshot = store.load_session(session.session_id)
    session, _, size = queue.enqueue(snapshot=snapshot, prompt="second", title="Second")

    snapshot = store.load_session(session.session_id)
    assert size == 2
    assert snapshot.session.metadata[QUEUED_INPUTS_KEY][0]["prompt"] == "first"
    assert snapshot.session.metadata[QUEUED_INPUTS_KEY][1]["title"] == "Second"

    first = queue.peek(snapshot)
    assert first is not None
    queue.acknowledge(snapshot, first.queue_id)
    snapshot = store.load_session(session.session_id)
    second = queue.peek(snapshot)
    assert second is not None
    queue.acknowledge(snapshot, second.queue_id)
    snapshot = store.load_session(session.session_id)

    assert first.prompt == "first"
    assert first.title is None
    assert second.prompt == "second"
    assert second.title == "Second"
    assert QUEUED_INPUTS_KEY not in snapshot.session.metadata


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


def test_queued_input_queue_requires_explicit_acknowledgement(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Queue")
    store.create_session(session)
    queue = QueuedInputQueue(store)
    queued_session, _, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="second",
        title=None,
    )
    snapshot = store.load_session(queued_session.session_id)

    first = queue.peek(snapshot)
    repeated = queue.peek(snapshot)

    assert first is not None
    assert repeated is not None
    assert repeated.queue_id == first.queue_id
    queue.acknowledge(snapshot, first.queue_id)
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
    )
    _, host_input, _ = queue.enqueue(
        snapshot=store.load_session(session.session_id),
        prompt="Framework continuation",
        title=None,
        source="host",
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
        )
    _, queued, _ = queue.enqueue(
        snapshot=snapshot,
        prompt="recover from checkpoint",
        title="Recovery",
        source="host",
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
        message_role="user",
        plugin_id="dev.example.plugin",
        contribution_id="command",
        generation_id="generation_1",
        execution_contract_hash="contract_1",
    )
    restored = queue.peek(store.load_session(queued_session.session_id))

    assert queued.source == "plugin"
    assert queued.message_role == "user"
    assert restored is not None
    assert restored.source == "plugin"
    assert restored.message_role == "user"
    assert restored.plugin_id == "dev.example.plugin"
    with pytest.raises(ValidationError, match="Host queued input must use the synthetic"):
        queue.enqueue(
            snapshot=store.load_session(session.session_id),
            prompt="Invalid Host role",
            title=None,
            source="host",
            message_role="user",
            host_source_id="ouro.runtime/test",
        )


def test_legacy_plugin_queue_defaults_to_synthetic_message_role(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(
        title="Legacy plugin queue",
        metadata={
            QUEUED_INPUTS_KEY: [
                {
                    "prompt": "legacy plugin continuation",
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

    assert restored is not None
    assert restored.message_role == "system_synthetic"


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
        )

    queued_session, queued, _ = queue.enqueue(
        snapshot=snapshot,
        prompt="Inspect the target.",
        title=None,
        source="delegation",
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
            )
            for index in range(20)
        ]
        for future in futures:
            future.result(timeout=2)

    reloaded = store.load_session(session.session_id)
    prompts = [item["prompt"] for item in reloaded.session.metadata[QUEUED_INPUTS_KEY]]
    assert sorted(prompts) == sorted(f"prompt-{index}" for index in range(20))
