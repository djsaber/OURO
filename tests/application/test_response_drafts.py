from __future__ import annotations

import asyncio
import multiprocessing
import os

import pytest

from ouro.application.observations.content import restore_session_content
from ouro.application.observations.events import SessionEventStream
from ouro.application.observations.sessions import observation_event
from ouro.application.runtime.service import RuntimeService
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.events import MODEL_STREAM_DELTA_EVENT_TYPES, EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.response_drafts import ResponseDraftChunk
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.response_drafts import persist_response_draft


@pytest.fixture(params=["files", "sqlite"])
def stores(tmp_path, request):
    def build():
        result = (
            LocalSessionStore(tmp_path)
            if request.param == "files"
            else SQLiteSessionStore(tmp_path / "sessions.db")
        )
        result.initialize()
        return result

    return build(), build()


def test_fact_page_reads_one_history_boundary(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    messages = [
        MessageRecord.text(session_id=session.session_id, role="assistant", text=f"answer {index}")
        for index in range(32)
    ]
    writer.commit_session(
        SessionCommit(
            before=writer.load_session_record(session.session_id),
            steps=tuple(SessionCommitStep(message=message) for message in messages),
            events=tuple(
                EventRecord(
                    session_id=session.session_id,
                    event_type="model_response_committed",
                    payload={"message_id": message.message_id},
                )
                for message in messages
            ),
        )
    )
    head_reads = 0
    load_head = reader.load_session_record

    def counted_head(session_id):
        nonlocal head_reads
        head_reads += 1
        return load_head(session_id)

    monkeypatch.setattr(reader, "load_session_record", counted_head)
    stream = SessionEventStream(
        reader,
        session.session_id,
        after_sequence=0,
        publish=lambda event: None,
        report=lambda diagnostic: pytest.fail(str(diagnostic)),
        is_running=lambda: False,
    )
    events, more = stream._read_page()
    assert not more
    assert [event.payload["text"] for event in events] == [
        message.text_content for message in messages
    ]
    assert head_reads == 1


async def test_cross_host_draft_snapshot_live_and_closed_cursor_converge(stores):
    writer, reader = stores
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="running")
    session.active_run_id = run.run_id
    session.status = "running"
    writer.create_session(session)
    writer.save_run(run)
    header = persist_response_draft(writer, run, turn_id="turn", text="prefix", reasoning="think")
    supervisor = RuntimeSupervisor(
        RuntimeService(
            store=reader,
            tools=ToolRegistry(),
            adapter=RuntimeAdapter(
                FauxProvider(), ModelMetadata(provider_id="test", model_id="test")
            ),
        )
    )
    observation = await supervisor.observe_session(session.session_id, max_buffer=2, page_size=2)
    try:
        initial = await observation.get()
        projection = restore_session_content(initial.snapshot)
        observation.acknowledge(initial)
        assert projection.assistant_text == "prefix"
        assert projection.reasoning_text == "think"
        writer.append_response_draft(
            session.session_id,
            header.context_id,
            ResponseDraftChunk(channel="text", content=" tail"),
        )
        async with asyncio.timeout(60):
            while projection.assistant_text != "prefix tail":
                batch = await observation.get()
                assert batch.diagnostic is None
                for event in batch.events:
                    projection.apply(observation_event(event))
                observation.acknowledge(batch)
        # Finish while another prefix is still unread. The cursor must converge
        # through the immutable response, not depend on retaining old fragments.
        writer.append_response_draft(
            session.session_id,
            header.context_id,
            ResponseDraftChunk(channel="text", content=" final"),
        )
        response = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id="turn",
            role="assistant",
            text="prefix tail final",
        )
        response.reasoning_content = "think"
        result = writer.commit_session(
            SessionCommit(
                before=writer.load_session_record(session.session_id),
                steps=(SessionCommitStep(message=response),),
            )
        )
        through = result.events[-1].sequence
        async with asyncio.timeout(60):
            while observation.applied_sequence < through:
                batch = await observation.get()
                assert batch.diagnostic is None
                for event in batch.events:
                    projection.apply(observation_event(event))
                observation.acknowledge(batch)
        assert projection.assistant_text == "prefix tail final"
        assert projection.reasoning_text == "think"
        assert reader.load_response_draft(session.session_id, header.context_id) is None
        assert all(
            event.event_type not in MODEL_STREAM_DELTA_EVENT_TYPES
            for event in writer.load_session(session.session_id).events
        )
    finally:
        observation.close()
        await supervisor.shutdown()


async def test_runtime_stream_fragments_are_current_only_and_live_sinks_still_receive_them(stores):
    store, _ = stores
    observed = []
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        event_sink=observed.append,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="reasoning_delta", reasoning_content="thinking"),
                    ProviderStreamEvent(type="text_delta", text="answer"),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(provider_id="test", model_id="test"),
        ),
    )
    try:
        result = await runtime.run("hello")
        await runtime.drain_events()
        snapshot = store.load_session(result.session.session_id)
        assert snapshot.messages[-1].text_content == "answer"
        assert any(
            event.event_type == "model_text_delta" and event.stream is not None
            for event in observed
        )
        assert all(
            event.event_type not in MODEL_STREAM_DELTA_EVENT_TYPES for event in snapshot.events
        )
        header = store.load_latest_context_header(result.session.session_id)
        assert header.response_closed_at is not None
        assert store.load_response_draft(result.session.session_id, header.context_id) is None
    finally:
        await runtime.close()


def test_recovery_reads_only_current_draft_and_never_turns_parameters_into_calls(
    stores, monkeypatch
):
    store, cold = stores
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="running")
    session.active_run_id = run.run_id
    session.status = "running"
    store.create_session(session)
    store.save_run(run)
    header = persist_response_draft(store, run, turn_id="turn", text="partial", reasoning="thought")
    # Provider done is an observation, not evidence of a committed response.
    store.settle_request_header(
        session.session_id,
        header.context_id,
        settled_at=utc_now(),
        provider_done=True,
        usage_entry_id=None,
    )
    store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="model_tool_call_delta",
            payload={"arguments_delta": "unexecutable"},
        )
    )

    def forbid(*args, **kwargs):
        pytest.fail("recovery scanned the event journal")

    monkeypatch.setattr(cold, "load_events_page", forbid)
    monkeypatch.setattr(cold, "load_events_after", forbid)
    recovery = SessionRecoveryService(cold)
    recovery.recover_runtime(session.session_id)
    response = cold.find_assistant_message(session.session_id, run.run_id, "turn")
    assert response.text_content == "partial" and response.reasoning_content == "thought"
    assert response.metadata["incomplete"] is True
    assert response.tool_calls == []
    assert cold.load_run(session.session_id, run.run_id).active_tool_batch is None
    assert cold.load_response_draft(session.session_id, header.context_id) is None
    recovery.recover_runtime(session.session_id)
    assert len(cold.load_session(session.session_id).messages) == 1


def _exit_after_prefix(root, backend, connection):
    store = (
        LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="running")
    session.status = "running"
    session.active_run_id = run.run_id
    store.create_session(session)
    store.save_run(run)
    header = persist_response_draft(
        store, run, turn_id="turn", text="process prefix", reasoning="process thought"
    )
    connection.send((session.session_id, run.run_id, header.context_id))
    os._exit(23)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_process_exit_recovers_only_acknowledged_current_prefix(tmp_path, backend):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_exit_after_prefix, args=(tmp_path, backend, sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(60), "prefix producer did not reach its commit boundary"
        session_id, run_id, context_id = receiver.recv()
        process.join(60)
        assert process.exitcode == 23
        store = (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.db")
        )
        store.initialize()
        recovered = SessionRecoveryService(store).recover_runtime(session_id)
        assert recovered.runs[0].status == "recoverable"
        message = store.find_assistant_message(session_id, run_id, "turn")
        assert message.text_content == "process prefix"
        assert message.reasoning_content == "process thought"
        assert message.metadata["incomplete"]
        assert store.load_response_draft(session_id, context_id) is None
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(60)
