from __future__ import annotations

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.application.sessions.recovery import SessionRecoveryService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionRecord
from ouro.core.context_providers import (
    ContextFragment,
    ContextProviderRegistration,
    ContextProviderRegistry,
    ContextProviderResult,
)
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.backends.files.messages import FileMessageRecords
from ouro.tools import ToolRegistry
from tests.response_drafts import persist_response_draft
from tests.session_summaries import commit_summary


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("covered", [4, 2048])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_cold_recovery_and_runtime_never_read_covered_message_bodies(
    tmp_path, monkeypatch, backend, covered, interrupted
):
    def open_store():
        return (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.db")
        )

    store = open_store()
    store.initialize()
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="running" if interrupted else "completed")
    session = session.model_copy(
        update={
            "status": "running" if interrupted else "idle",
            "active_run_id": run.run_id if interrupted else None,
            "last_run_id": run.run_id,
        }
    )
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="user" if i % 2 == 0 else "assistant",
            text=f"message {i}",
        )
        for i in range(covered + 2)
    ]
    store.create_session_snapshot(session, messages)
    store.save_run(run)
    commit_summary(
        store,
        CompactionRecord(
            session_id=session.session_id,
            strategy_id="test",
            trigger="manual",
            summary="covered summary",
            covered_message_count=covered,
            covered_end_message_id=messages[covered - 1].message_id,
            history_boundary_message_id=messages[-1].message_id,
        ),
    )
    if interrupted:
        persist_response_draft(store, run, turn_id="turn_interrupted", text="partial response")
    covered_ids = {message.message_id for message in messages[:covered]}
    reads = []
    if backend == "files":
        fact = FileMessageRecords.fact

        def read_fact(records, directory, message_id):
            assert message_id not in covered_ids
            reads.append(message_id)
            return fact(records, directory, message_id)

        monkeypatch.setattr(FileMessageRecords, "fact", read_fact)
        entry = FileMessageRecords.entry

        def read_entry(payload, *, session_id):
            selected = entry(payload, session_id=session_id)
            assert selected.message_id not in covered_ids
            reads.append(selected.message_id)
            return selected

        monkeypatch.setattr(FileMessageRecords, "entry", staticmethod(read_entry))
    else:
        import ouro.sessions.backends.sqlite.store as sqlite_store

        decode = sqlite_store.decode_message_content

        def read_message(payload, *, label):
            message = decode(payload, label=label)
            assert message.message_id not in covered_ids
            reads.append(message.message_id)
            return message

        monkeypatch.setattr(sqlite_store, "decode_message_content", read_message)

    # A fresh adapter must not populate a historical body cache before executing.
    cold = open_store()
    cold.initialize()
    recovered = SessionRecoveryService(cold).recover_runtime(session.session_id)
    assert recovered.runs[0].status == ("recoverable" if interrupted else "completed")
    assert recovered.session.message_count == covered + 2 + int(interrupted)
    observed = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            observed.extend(message.text_content for message in kwargs["messages"])
            async for event in super().stream(**kwargs):
                yield event

    runtime = RuntimeService(
        store=cold,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(Provider(), ModelMetadata(model_id="test", provider_id="test")),
    )
    try:
        result = await runtime.run("new task", session_id=session.session_id)
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "Hello from OURO."
        assert "covered summary" in observed[0]
        assert observed[1:3] == [
            f"message {covered}",
            f"message {covered + 1}",
        ]
        assert observed[-1] == "new task"
        assert len(observed) == 4 + int(interrupted)
        if interrupted:
            assert observed[3].startswith("<interrupted-assistant-attempt>")
            assert "partial response" in observed[3]
            assert "does not confirm" in observed[3]
        assert reads
        assert cold.load_session_record(session.session_id).message_count == (
            covered + 4 + int(interrupted)
        )
    finally:
        await runtime.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("covered", [4, 2048])
async def test_preparation_reads_only_the_selected_suffix(tmp_path, monkeypatch, backend, covered):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user" if i % 2 == 0 else "assistant",
            text=f"message {i}",
        )
        for i in range(covered + 2)
    ]
    store.create_session_snapshot(session, messages)
    summary = CompactionRecord(
        session_id=session.session_id,
        strategy_id="test",
        trigger="manual",
        summary="covered summary",
        covered_message_count=covered,
        covered_end_message_id=messages[covered - 1].message_id,
        history_boundary_message_id=messages[-1].message_id,
    )
    commit_summary(store, summary)
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    session = store.load_session_record(session.session_id).model_copy(
        update={"active_run_id": run.run_id, "status": "running"}
    )
    store.save_session(session)
    counts = []

    def context_provider(call):
        counts.append(call.message_count)
        return ContextProviderResult()

    providers = ContextProviderRegistry()
    providers.replace_owner(
        "test",
        (
            ContextProviderRegistration(
                plugin_id="dev.ouro.test",
                provider_id="count",
                handler=context_provider,
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        context_providers=providers,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="test")),
    )
    original_tail = store.load_messages_tail
    reads = []

    def selected_tail(session_id, *, limit=100, include_media=True):
        reads.append(limit)
        assert limit == 2
        return original_tail(session_id, limit=limit, include_media=include_media)

    def forbid_history(*args, **kwargs):
        pytest.fail("request preparation loaded unrelated history")

    monkeypatch.setattr(store, "load_messages_tail", selected_tail)
    for name in ("load_session", "load_runtime_session", "load_compactions", "load_run_messages"):
        monkeypatch.setattr(store, name, forbid_history)
    try:
        view, _ = await runtime.context_preparation.prepare(
            0,
            session=session,
            run=run,
            tools=[],
            model_round=1,
            turn_id="turn_selected",
            abort_signal=AbortSignal(),
        )
        assert counts == [covered + 2]
        assert reads == [2]
        assert "covered summary" in view.provider_context.messages[0].text_content
        assert [message.text_content for message in view.provider_context.messages[1:]] == [
            f"message {covered}",
            f"message {covered + 1}",
        ]
        latest = store.load_latest_context(session.session_id)
        assert latest is not None
        assert latest.provider_context == view.provider_context
    finally:
        await runtime.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_persistent_fragment_retries_use_the_last_value_even_after_compaction(
    tmp_path, monkeypatch, backend
):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    session = store.load_session_record(session.session_id).model_copy(
        update={"active_run_id": run.run_id, "status": "running"}
    )
    store.save_session(session)
    values = ["first state", "retained state"]
    providers = ContextProviderRegistry()
    providers.replace_owner(
        "test",
        (
            ContextProviderRegistration(
                plugin_id="dev.ouro.test",
                provider_id="state",
                handler=lambda _: ContextProviderResult(
                    fragments=[
                        ContextFragment(text=value, persistence="message") for value in values
                    ]
                ),
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        context_providers=providers,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="test")),
    )

    def forbid_scan(*args, **kwargs):
        pytest.fail("fragment retries must use their delivery identity, not a Run history scan")

    monkeypatch.setattr(store, "load_run_messages", forbid_scan)

    async def prepare():
        return await runtime.context_preparation.prepare(
            0,
            session=session,
            run=run,
            tools=[],
            model_round=1,
            turn_id="turn_retry",
            abort_signal=AbortSignal(),
        )

    try:
        await prepare()
        first, last = reversed(store.load_messages_tail(session.session_id))
        commit_summary(
            store,
            CompactionRecord(
                session_id=session.session_id,
                strategy_id="test",
                trigger="manual",
                summary="previous state",
                covered_message_count=1,
                covered_end_message_id=first.message_id,
                history_boundary_message_id=last.message_id,
            ),
        )
        await prepare()
        assert store.load_session_record(session.session_id).message_count == 2
        values[0] = "changed state"
        await prepare()
        assert store.load_session_record(session.session_id).message_count == 3
        newest = store.load_messages_tail(session.session_id, limit=1)[0]
        assert newest.text_content == "changed state"
        assert (
            newest.metadata["context_provider_delivery_key"]
            == first.metadata["context_provider_delivery_key"]
        )
        await prepare()
        assert store.load_session_record(session.session_id).message_count == 3
    finally:
        await runtime.close()
