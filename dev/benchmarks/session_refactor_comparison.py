"""Compare isolated pre-refactor and working-tree sources with identical workloads.

Run with the locked interpreter. JSONL stdout is the measurement record; no
benchmark homes or source snapshots survive the temporary-directory lifetime.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import tracemalloc
from contextlib import closing
from pathlib import Path


def storage(root, backend):
    paths = [path for path in root.rglob("*") if path.is_file()]
    result = {"files": len(paths), "bytes": sum(path.stat().st_size for path in paths)}
    database = (
        root / "sessions.db"
        if backend == "sqlite"
        else next(root.glob("sessions/*/session-data.sqlite3"), None)
    )
    if database is not None:
        with closing(sqlite3.connect(database)) as connection:
            for name in ("page_count", "page_size", "freelist_count"):
                result[name] = connection.execute(f"PRAGMA {name}").fetchone()[0]
        result["used_page_bytes"] = (result["page_count"] - result["freelist_count"]) * result[
            "page_size"
        ]
    return result


def rss():
    from dev.benchmarks.session_observation import process_resources

    return process_resources().get("working_set_bytes")


def distribution(values):
    ordered = sorted(values)
    return {
        **({"samples": values} if len(values) <= 32 else {}),
        "count": len(values),
        "median": statistics.median(ordered),
        "p95": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        "max": ordered[-1],
    }


def adapter(root, backend):
    from ouro.sessions import LocalSessionStore, SQLiteSessionStore

    return (
        LocalSessionStore(root) if backend == "files" else SQLiteSessionStore(root / "sessions.db")
    )


def make_runtime(store, provider, *, long_run=False, structured_bytes=0, argument_bytes=0):
    from ouro.application.runtime.service import RuntimeService
    from ouro.config.compaction import CompactionConfig
    from ouro.core.adapter import RuntimeAdapter
    from ouro.core.compaction import (
        CompactionArtifact,
        CompactionPolicy,
        CompactionStrategyRegistry,
    )
    from ouro.core.provider import ModelMetadata
    from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult

    class Summary:
        strategy_id, version = "benchmark.fixed-summary", "1"
        count = 0

        async def compact(self, request, *, signal=None):
            self.count += 1
            return CompactionArtifact(summary="Prior evidence retained; continue remaining work.")

    summary = Summary()
    strategies, tools = CompactionStrategyRegistry(), ToolRegistry()
    strategies.register(summary)
    if long_run:
        tools.register(
            Tool(
                definition=ToolDefinition(
                    name="Evidence", description="Return synthetic evidence", effect="read_only"
                ),
                handler=lambda invocation, context: ToolResult(
                    tool_call_id=invocation.tool_call_id,
                    tool_name=invocation.tool_name,
                    content="y" * 4096,
                    structured={"detail": "z" * structured_bytes} if structured_bytes else {},
                ),
            )
        )
    runtime = RuntimeService(
        store=store,
        tools=tools,
        system_prompt="",
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(
                model_id="comparison",
                provider_id=provider.provider_id,
                context_window=5000 + argument_bytes if long_run else 32768,
                max_output_tokens=1000,
            ),
        ),
        compaction_strategies=strategies,
        compaction_config=CompactionConfig(
            strategy_id=summary.strategy_id,
            policy=CompactionPolicy(trigger_ratio=0.7, target_ratio=0.35),
        ),
    )
    return runtime, summary


async def long_run(
    root,
    backend,
    rounds,
    *,
    observe=False,
    structured_bytes=0,
    harness_owner_bytes=0,
    argument_bytes=0,
):
    from dev.benchmarks.harness_lifecycle import process_io
    from ouro.core.harness import HarnessState
    from ouro.core.provider import ProviderStreamEvent
    from ouro.core.session_commits import SessionCommit, SessionCommitStep
    from ouro.core.session_extensions import SessionExtensionStateRecord

    class Provider:
        provider_id = "comparison"

        def __init__(self):
            self.boundaries, self.memory, self.input_sizes = [], [], []

        async def stream(self, *, messages, **kwargs):
            self.boundaries.append(time.perf_counter())
            self.memory.append(rss())
            self.input_sizes.append(sum(len(message.text_content) for message in messages))
            index = len(self.boundaries)
            for _ in range(8):
                yield ProviderStreamEvent(type="text_delta", text="x" * 512)
            if index <= rounds:
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": f"call_{index}",
                        "name": "Evidence",
                        "arguments": {"evidence": "a" * argument_bytes} if argument_bytes else {},
                    }
                )
            yield ProviderStreamEvent(type="done")

    store = adapter(root, backend)
    store.initialize()
    provider = Provider()
    runtime, summary = make_runtime(
        store,
        provider,
        long_run=True,
        structured_bytes=structured_bytes,
        argument_bytes=argument_bytes,
    )
    session = runtime.create_session(title="Synthetic long Run")
    if harness_owner_bytes:
        state = HarnessState(
            states=tuple(
                SessionExtensionStateRecord(
                    session_id=session.session_id,
                    owner_id=f"example.owner{index}",
                    state_schema_version=1,
                    payload={"value": "h" * harness_owner_bytes},
                )
                for index in range(8)
            )
        )
        session = store.commit_session(
            SessionCommit(before=session, steps=(SessionCommitStep(harness=state),))
        ).session
    keeper = sqlite3.connect(root / "sessions.db") if backend == "sqlite" else None
    if keeper is not None:
        keeper.execute("SELECT count(*) FROM sqlite_master").fetchone()
    gc.collect()
    before_rss = rss()
    before_io = process_io()
    started = time.perf_counter()
    try:
        result = await runtime.run("Gather evidence", session_id=session.session_id)
        elapsed = time.perf_counter() - started
        after_io = process_io()
        assert result.run.status == "completed", (result.run.status, result.run.error)
        assert len(provider.boundaries) == rounds + 1
        assert result.final_text == "x" * 4096
        assert store.load_session_record(session.session_id).message_count == 2 * rounds + 2
        await runtime.drain_events()
        after_rss = rss()
        wal = root / "sessions.db-wal"
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        await runtime.close()
        if keeper is not None:
            assert keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            keeper.close()
        intervals = [
            b - a for a, b in zip(provider.boundaries, provider.boundaries[1:], strict=False)
        ]
        report = {
            "structured_bytes_per_result": structured_bytes,
            "argument_bytes_per_call": argument_bytes,
            "harness_owner_bytes": harness_owner_bytes,
            "seconds": elapsed,
            "process_io": {key: after_io[key] - before_io[key] for key in before_io},
            "round_seconds": intervals,
            "last_8_round_median": statistics.median(intervals[-8:]),
            "rss_before": before_rss,
            "rss_sampled_max": max(value for value in provider.memory if value is not None)
            if before_rss is not None
            else None,
            "rss_after_run": after_rss,
            "summaries": summary.count,
            "input_chars_max": max(provider.input_sizes),
            "run_result_bytes": len(result.model_dump_json().encode()),
            "run_header_bytes": len(result.run.model_dump_json().encode()),
            "wal_before_checkpoint": wal_bytes,
            "storage": storage(root, backend),
        }
        if observe:
            observed = store.load_session_observation(session.session_id)
            result_id = next(
                message.message_id
                for message in reversed(observed.messages)
                if message.role == "tool_result"
            )
            del observed
            report.update(
                reads_seconds=query_reads(
                    root, backend, session.session_id, result_message_id=result_id
                ),
                cadence_io=await stream_io(root, backend, session.session_id, mode="cadence"),
                sustained_io=await stream_io(root, backend, session.session_id, mode="sustained"),
            )
        return report
    finally:
        await runtime.close()
        if keeper is not None:
            keeper.close()


def seed_history(root, backend, count, variant):
    from ouro.core.compaction import CompactionRecord
    from ouro.core.messages import MessageRecord
    from ouro.core.state import SessionRecord

    store = adapter(root, backend)
    store.initialize()
    session = SessionRecord(session_id="ses_comparison")
    messages = [
        MessageRecord(
            session_id=session.session_id,
            message_id=f"msg_{index:08d}",
            sequence=index + 1,
            before_harness_version=session.harness_version,
            role="user" if index % 2 == 0 else "assistant",
            content=[{"type": "text", "text": "h" * 1024}],
        )
        for index in range(count)
    ]
    covered = count - 8
    fields = dict(
        session_id=session.session_id,
        compaction_id="cmp_comparison",
        strategy_id="benchmark.fixed-summary",
        trigger="manual",
        summary="Prior evidence retained; continue remaining work.",
        history_boundary_message_id=messages[-1].message_id,
    )
    if variant == "before":
        from ouro.core.compaction import compaction_source_digest

        fields.update(
            covered_message_ids=[message.message_id for message in messages[:covered]],
            source_digest=compaction_source_digest(messages[:covered]),
        )
        session.active_compaction_id = fields["compaction_id"]
    else:
        fields.update(
            covered_message_count=covered,
            covered_end_message_id=messages[covered - 1].message_id,
        )
        session.context_selection.compaction_id = fields["compaction_id"]
    store.create_session_snapshot(session, messages, compactions=[CompactionRecord(**fields)])
    return session.session_id


async def stream_io(root, backend, session_id, *, mode="paced", sustained_chunks=8192):
    from ouro.application.observations.events import SessionEventStream
    from ouro.core.provider import ProviderStreamEvent

    local, remote, starts = [], [], []
    observed, diagnostics = asyncio.Event(), []
    chunk_count = {"paced": 32, "burst": 128, "cadence": 256, "sustained": sustained_chunks}[mode]
    chunks = [f"{index:08x}:" + "s" * 247 for index in range(chunk_count)]
    received, local_text = [], []
    completed = {}
    byte_counts = {False: 0, True: 0}

    class Provider:
        provider_id = "comparison"

        async def stream(self, **kwargs):
            for chunk in chunks:
                if mode == "paced":
                    observed.clear()
                starts.append(time.perf_counter())
                yield ProviderStreamEvent(type="text_delta", text=chunk)
                if mode == "paced":
                    async with asyncio.timeout(60):
                        await observed.wait()
                elif mode == "cadence":
                    await asyncio.sleep(0.02)
            if mode != "paced":
                async with asyncio.timeout(60):
                    await observed.wait()
            yield ProviderStreamEvent(type="done")

    def receive(event, timings, *, acknowledge=False):
        if event.event_type == "model_text_delta":
            target = received if acknowledge else local_text
            content = event.payload["text"]
            target.append(content)
            now = time.perf_counter()
            byte_counts[acknowledge] += len(content)
            through = byte_counts[acknowledge] // 256
            timings.extend(now - starts[index] for index in range(len(timings), through))
            if through == chunk_count:
                completed["read" if acknowledge else "write"] = now
            if acknowledge and (mode == "paced" or through == chunk_count):
                observed.set()

    runtime, _ = make_runtime(adapter(root, backend), Provider())
    registration = runtime.event_recorder.add_event_sink(lambda event: receive(event, local))
    reader = adapter(root, backend)
    observer = SessionEventStream(
        reader,
        session_id,
        after_sequence=reader.latest_event_sequence(session_id),
        publish=lambda event: receive(event, remote, acknowledge=True),
        report=diagnostics.append,
        is_running=lambda: True,
    )
    task = asyncio.create_task(observer.follow())
    try:
        run_started = time.perf_counter()
        result = await runtime.run("Stream evidence", session_id=session_id)
        assert result.run.status == "completed", result.run.error
        await runtime.drain_events()
        run_finished = time.perf_counter()
        assert not diagnostics, diagnostics
        assert len(local) == len(remote) == chunk_count
        if mode == "paced":
            return {"local_seconds": distribution(local), "reader_seconds": distribution(remote)}
        assert "".join(received) == "".join(chunks)
        assert "".join(local_text) == "".join(chunks)
        write_seconds = completed["write"] - starts[0]
        read_seconds = completed["read"] - starts[0]
        return {
            "chunks": chunk_count,
            "bytes": chunk_count * 256,
            "local_deliveries": len(local_text),
            "reader_deliveries": len(received),
            "write_seconds": write_seconds,
            "reader_seconds": read_seconds,
            "write_bytes_per_second": chunk_count * 256 / write_seconds,
            "reader_bytes_per_second": chunk_count * 256 / read_seconds,
            "run_seconds": run_finished - run_started,
            "finalization_seconds": run_finished - max(completed.values()),
            "local_latency_seconds": distribution(local),
            "reader_latency_seconds": distribution(remote),
        }
    finally:
        observer.close()
        await task
        await registration.close()
        await runtime.close()


async def streaming_history(root, backend, count, variant, *, chunks):
    session_id = seed_history(root, backend, count, variant)
    before = storage(root, backend)
    result = await stream_io(root, backend, session_id, mode="sustained", sustained_chunks=chunks)
    return {
        "storage_before": before,
        "sustained_io": result,
        "storage_after": storage(root, backend),
    }


def query_reads(root, backend, session_id, *, result_message_id=None):
    reads = {}
    operations = [
        ("head", lambda store: store.load_session_record(session_id)),
        ("tail_16", lambda store: store.load_messages_tail(session_id, limit=16)),
        ("observation", lambda store: store.load_session_observation(session_id)),
    ]
    if result_message_id is not None:
        operations.append(
            ("tool_result", lambda store: store.load_message(session_id, result_message_id))
        )
    for name, operation in operations:
        values = []
        for _ in range(3):
            store = adapter(root, backend)
            started = time.perf_counter()
            value = operation(store)
            values.append(time.perf_counter() - started)
            del value, store
        reads[name] = distribution(values)
    return reads


async def interaction(root, backend, count, variant):
    """Measure Host-facing reads and commands, excluding rendering and network time."""
    from dev.benchmarks.harness_lifecycle import process_io
    from ouro.application.inputs.requests import UserInputRequest
    from ouro.application.sessions.service import SessionService
    from ouro.application.supervisor import RuntimeSupervisor
    from ouro.core.provider import ProviderStreamEvent

    session_id = seed_history(root, backend, count, variant)
    initial_storage = storage(root, backend)
    reads = {name: [] for name in ("open_200", "previous_200", "reopen_200")}
    read_io = {name: [] for name in reads}
    page_sizes = {}

    async def read(name, operation, *args, **kwargs):
        before = process_io()
        started = time.perf_counter()
        value = await asyncio.to_thread(operation, *args, **kwargs)
        reads[name].append(time.perf_counter() - started)
        after = process_io()
        read_io[name].append({key: after[key] - before[key] for key in before})
        page_sizes[name] = len(value.messages)
        return value

    for _ in range(3):
        service = SessionService(adapter(root, backend))
        snapshot = await read(
            "open_200", service.observation_snapshot, session_id, history_limit=200
        )
        assert len(snapshot.messages) == min(count, 200)
        if snapshot.history_before_cursor is not None:
            page = await read(
                "previous_200",
                service.history_window,
                session_id,
                limit=200,
                before=snapshot.history_before_cursor,
                history_epoch=snapshot.session.history_epoch,
            )
            assert not {item.message_id for item in page.messages}.intersection(
                item.message_id for item in snapshot.messages
            )
        service = SessionService(adapter(root, backend))
        reopened = await read(
            "reopen_200", service.observation_snapshot, session_id, history_limit=200
        )
        assert reopened.messages == snapshot.messages

    class Provider:
        provider_id = "comparison"

        def __init__(self):
            self.hold = False
            self.release = asyncio.Event()
            self.started_at = self.closed_at = 0.0
            self.requests = 0

        async def stream(self, *, request_context, **kwargs):
            assert request_context.values["request.purpose"] == "chat"
            self.requests += 1
            self.started_at = time.perf_counter()
            try:
                yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="检查上下文。")
                yield ProviderStreamEvent(
                    type="text_delta", text="已收到。\n\n```python\n1 + 1\n```\n"
                )
                if self.hold:
                    await self.release.wait()
                yield ProviderStreamEvent(type="done")
            finally:
                self.closed_at = time.perf_counter()

    provider = Provider()
    runtime, _ = make_runtime(adapter(root, backend), provider)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    first_text = asyncio.Event()
    first_text_at = 0.0

    def receive(event):
        nonlocal first_text_at
        if event.event_type == "model_text_delta" and not first_text.is_set():
            first_text_at = time.perf_counter()
            first_text.set()

    registration = runtime.event_recorder.add_event_sink(receive)
    commands = {}
    try:
        await supervisor.start()
        for name, target in (("new_session", None), ("continue_history", session_id)):
            first_text.clear()
            started = time.perf_counter()
            submission = await supervisor.submit_user_input(
                UserInputRequest(prompt="请继续检查上下文，并给出简短答复。", session_id=target)
            )
            accepted_at = time.perf_counter()
            result = await submission.wait()
            finished_at = time.perf_counter()
            await runtime.drain_events()
            assert result.run.status == "completed", result.run.error
            assert first_text.is_set() and result.final_text.startswith("已收到。")
            commands[name] = {
                "accepted_seconds": accepted_at - started,
                "model_entered_seconds": provider.started_at - started,
                "first_text_seconds": first_text_at - started,
                "completed_seconds": finished_at - started,
            }

        provider.hold = True
        first_text.clear()
        submission = await supervisor.submit_user_input(
            UserInputRequest(prompt="保持输出，等待取消。", session_id=session_id)
        )
        async with asyncio.timeout(60):
            await first_text.wait()
        service = SessionService(adapter(root, backend))
        started = time.perf_counter()
        snapshot = await asyncio.to_thread(
            service.observation_snapshot, session_id, history_limit=200
        )
        commands["open_running_seconds"] = time.perf_counter() - started
        assert snapshot.session.active_run_id is not None
        assert any(
            event.event_type == "model_text_delta" and "已收到。" in event.payload["text"]
            for event in snapshot.events
        )
        requests_before = provider.requests
        started = time.perf_counter()
        queued = await supervisor.submit_user_input(
            UserInputRequest(prompt="取消前追加的排队输入。", session_id=session_id)
        )
        commands["queue_accept_seconds"] = time.perf_counter() - started
        pending = await asyncio.to_thread(runtime.pending_user_inputs, session_id)
        assert [item.queue_id for item in pending] == [queued.receipt.queue_id]
        assert provider.requests == requests_before
        started = time.perf_counter()
        cancelled = await runtime.cancel_pending_user_input(session_id, queued.receipt.queue_id)
        commands["queue_cancel_seconds"] = time.perf_counter() - started
        assert cancelled is not None
        assert not await asyncio.to_thread(runtime.pending_user_inputs, session_id)

        started = time.perf_counter()
        await supervisor.abort(session_id)
        commands["abort_acknowledged_seconds"] = time.perf_counter() - started
        result = await submission.wait()
        commands["abort_completed_seconds"] = time.perf_counter() - started
        commands["abort_provider_closed_seconds"] = provider.closed_at - started
        assert result.run.status == "aborted", result.run.status
        assert provider.closed_at >= started and provider.requests == requests_before
        return {
            "history_messages": count,
            "storage": initial_storage,
            "navigation_seconds": {
                name: distribution(values) if values else None for name, values in reads.items()
            },
            "navigation_process_io": read_io,
            "page_message_counts": page_sizes,
            "commands": commands,
        }
    finally:
        provider.release.set()
        await supervisor.shutdown()
        await registration.close()
        await runtime.close()


async def history(root, backend, count, variant):
    from ouro.application.sessions.recovery import SessionRecoveryService
    from ouro.core.provider import ProviderStreamEvent

    started = time.perf_counter()
    session_id = seed_history(root, backend, count, variant)
    setup_seconds = time.perf_counter() - started
    initial_storage = storage(root, backend)
    gc.collect()
    fresh = adapter(root, backend)
    started = time.perf_counter()
    recovered = SessionRecoveryService(fresh).recover_runtime(session_id)
    recovery_seconds = time.perf_counter() - started
    assert recovered.session.message_count == count
    del recovered, fresh
    gc.collect()
    tracemalloc.start()
    recovered = SessionRecoveryService(adapter(root, backend)).recover_runtime(session_id)
    _, recovery_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del recovered
    reads = query_reads(root, backend, session_id)

    class Provider:
        provider_id = "comparison"
        input_chars = 0
        input_messages = 0

        async def stream(self, *, messages, **kwargs):
            self.input_chars = sum(len(message.text_content) for message in messages)
            self.input_messages = len(messages)
            yield ProviderStreamEvent(type="text_delta", text="answer" * 128)
            yield ProviderStreamEvent(type="done")

    provider = Provider()
    runtime, _ = make_runtime(adapter(root, backend), provider)
    try:
        started = time.perf_counter()
        result = await runtime.run("Continue", session_id=session_id)
        turn_seconds = time.perf_counter() - started
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "answer" * 128
    finally:
        await runtime.close()
    return {
        "setup_seconds": setup_seconds,
        "storage": initial_storage,
        "recovery_seconds": recovery_seconds,
        "recovery_python_peak_bytes": recovery_peak,
        "reads_seconds": reads,
        "turn_seconds": turn_seconds,
        "input_chars": provider.input_chars,
        "input_messages": provider.input_messages,
        "stream_io": await stream_io(root, backend, session_id),
        "burst_io": await stream_io(root, backend, session_id, mode="burst"),
    }


def request_io(root, backend, size, replacements, variant, *, content):
    from dev.benchmarks.harness_lifecycle import process_io
    from ouro.core.context import ContextMetrics
    from ouro.core.state import SessionRecord

    store = adapter(root, backend)
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    owner = store.acquire_session_lock(session.session_id, "run")
    assert owner is not None
    durations, samples, transferred = [], [], []
    try:
        for index in range(replacements):
            character = "x" if content == "stable" else chr(65 + index % 26)
            prompt = f"{index}:" + character * size
            fields = dict(
                context_id=f"ctx_{index}",
                session_id=session.session_id,
                run_id="run",
                turn_id="turn",
                model_round=index + 1,
                provider_id="test",
                model_id="test",
                metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=1),
            )
            if variant == "before":
                from ouro.core.context import ContextBuilder
                from ouro.core.context_snapshots import ContextSnapshotRecord, context_view_digest

                view = ContextBuilder().build(messages=[], tools=[], system_prompt=prompt)
                request = ContextSnapshotRecord(
                    **fields,
                    system_prompt=prompt,
                    context_digest=context_view_digest(view),
                )
                publish = store.save_latest_context
            else:
                from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
                from ouro.core.request_manifest import RequestManifest
                from ouro.core.session_commits import SessionCommit

                request = SessionCommit(
                    before=store.load_session_record(session.session_id),
                    request_header=ModelRequestHeader(**fields),
                    request_manifest=RequestManifestRecord(
                        context_id=fields["context_id"],
                        manifest=RequestManifest(system_prompt=prompt),
                    ),
                )
                publish = store.commit_session
            before_io = process_io()
            started = time.perf_counter()
            publish(request)
            durations.append(time.perf_counter() - started)
            after_io = process_io()
            transferred.append({key: after_io[key] - before_io[key] for key in after_io})
            samples.append({"replacement": index + 1, **storage(root, backend)})
        current = store.load_latest_context(session.session_id)
        assert current.context_id == fields["context_id"]
        retained = (
            current.system_prompt if variant == "before" else current.provider_context.system_prompt
        )
        assert retained == prompt
    finally:
        owner.release()
    return {
        "input_text_bytes": size,
        "request_content": content,
        "replacements": replacements,
        "preparation_seconds": distribution(durations),
        "process_io": transferred,
        "storage_after_each_commit": samples,
        "storage_after_owner_release": storage(root, backend),
        "scope": (
            "request publication only; execution owner retained; no forced checkpoint; "
            "fixture/model construction excluded; cached process IO, not physical disk"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="44724d8378632f3c5e63b6861e5e873493e71dce")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--rounds", type=int, nargs="+", default=[8, 32, 64])
    parser.add_argument("--messages", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument(
        "--scenario",
        choices=[
            "all",
            "long_run",
            "history",
            "agent_io",
            "stream_io",
            "request_io",
            "interaction",
        ],
        default="all",
    )
    parser.add_argument("--request-bytes", type=int, nargs="+", default=[4096, 1048576, 4194304])
    parser.add_argument("--request-replacements", type=int, default=32)
    parser.add_argument("--request-content", choices=["stable", "changing"], default="changing")
    parser.add_argument("--argument-bytes", type=int, default=0)
    parser.add_argument("--stream-chunks", type=int, default=8192)
    parser.add_argument(
        "--structured-bytes",
        type=int,
        default=0,
        help="Structured detail bytes per Evidence result",
    )
    parser.add_argument(
        "--harness-owner-bytes",
        type=int,
        default=0,
        help="Unchanged private bytes per Harness owner (eight owners) during long_run/agent_io",
    )
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--variant", choices=["before", "after"], help=argparse.SUPPRESS)
    parser.add_argument(
        "--backend", choices=["files", "sqlite"], help="Measure one backend (default: both)"
    )
    parser.add_argument("--size", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    backends = (args.backend,) if args.backend is not None else ("files", "sqlite")
    project = Path(__file__).resolve().parents[2]
    if args.worker_root:
        sys.path.insert(0, str(project))
        sys.path.insert(0, str(args.worker_root))
        import ouro

        assert Path(ouro.__file__).resolve().is_relative_to(args.worker_root)
        with tempfile.TemporaryDirectory(prefix="ouro-comparison-data-") as home:
            if args.scenario == "request_io":
                report = request_io(
                    Path(home),
                    args.backend,
                    args.size,
                    args.request_replacements,
                    args.variant,
                    content=args.request_content,
                )
                print(json.dumps(report), flush=True)
                return
            if args.scenario in {"long_run", "agent_io"}:
                operation = long_run(
                    Path(home),
                    args.backend,
                    args.size,
                    observe=args.scenario == "agent_io",
                    structured_bytes=args.structured_bytes,
                    harness_owner_bytes=args.harness_owner_bytes,
                    argument_bytes=args.argument_bytes,
                )
            elif args.scenario == "interaction":
                operation = interaction(Path(home), args.backend, args.size, args.variant)
            elif args.scenario == "stream_io":
                operation = streaming_history(
                    Path(home), args.backend, args.size, args.variant, chunks=args.stream_chunks
                )
            else:
                operation = history(Path(home), args.backend, args.size, args.variant)
            report = asyncio.run(operation)
        print(json.dumps(report), flush=True)
        return
    if (
        args.repeat < 1
        or min(args.rounds) < 1
        or min(args.messages) < 10
        or args.stream_chunks < 1
        or args.structured_bytes < 0
        or args.harness_owner_bytes < 0
        or args.argument_bytes < 0
        or min(args.request_bytes) < 1
        or args.request_replacements < 1
    ):
        parser.error("repeat/rounds/stream-chunks must be positive; messages must be at least ten")
    git = ["git", "-C", str(project), "-c", "core.safecrlf=false"]
    baseline = subprocess.check_output([*git, "rev-parse", args.baseline], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="ouro-comparison-source-") as temporary:
        sources = Path(temporary)
        before, after = sources / "before", sources / "after"
        before.mkdir()
        with tempfile.TemporaryFile() as stream:
            subprocess.run([*git, "archive", baseline, "ouro"], stdout=stream, check=True)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                archive.extractall(before, filter="data")
        paths = subprocess.check_output(
            [*git, "ls-files", "--cached", "--others", "--exclude-standard", "ouro"], text=True
        ).splitlines()
        digest = hashlib.sha256()
        for name in sorted(set(paths)):
            source = project / name
            if not source.is_file():
                continue
            content = source.read_bytes()
            digest.update(name.encode() + b"\0" + content)
            target = after / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        print(
            json.dumps(
                {
                    "environment": {
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                        "sqlite": sqlite3.sqlite_version,
                        "cpu_count": os.cpu_count(),
                        "baseline": baseline,
                        "after_ouro_sha256": digest.hexdigest(),
                        "repeat": args.repeat,
                        "backends": backends,
                        "rounds": args.rounds,
                        "messages": args.messages,
                        "stream_chunks": args.stream_chunks,
                        "structured_bytes": args.structured_bytes,
                        "harness_owner_bytes": args.harness_owner_bytes,
                        "argument_bytes": args.argument_bytes,
                        "request_bytes": args.request_bytes,
                        "request_replacements": args.request_replacements,
                        "request_content": args.request_content,
                        "cache": "fresh process and adapter; OS cache not cleared",
                    }
                }
            ),
            flush=True,
        )
        scenarios = ["long_run", "history"] if args.scenario == "all" else [args.scenario]
        for repeat in range(args.repeat):
            for scenario in scenarios:
                sizes = (
                    args.request_bytes
                    if scenario == "request_io"
                    else args.messages
                    if scenario in {"history", "stream_io", "interaction"}
                    else args.rounds
                )
                for backend in backends:
                    for size in sizes:
                        # Alternate order to reduce systematic cache/thermal ordering bias.
                        variants = ["before", "after"] if repeat % 2 == 0 else ["after", "before"]
                        for variant in variants:
                            identity = dict(
                                repeat=repeat,
                                scenario=scenario,
                                backend=backend,
                                size=size,
                                variant=variant,
                            )
                            print(json.dumps({"starting": identity}), flush=True)
                            command = [
                                sys.executable,
                                "-B",
                                "-X",
                                "utf8",
                                str(Path(__file__).resolve()),
                                "--worker-root",
                                str(sources / variant),
                                "--variant",
                                variant,
                                "--backend",
                                backend,
                                "--scenario",
                                scenario,
                                "--size",
                                str(size),
                                "--stream-chunks",
                                str(args.stream_chunks),
                                "--structured-bytes",
                                str(args.structured_bytes),
                                "--harness-owner-bytes",
                                str(args.harness_owner_bytes),
                                "--argument-bytes",
                                str(args.argument_bytes),
                                "--request-replacements",
                                str(args.request_replacements),
                                "--request-content",
                                args.request_content,
                            ]
                            report = json.loads(
                                subprocess.check_output(
                                    command, text=True, encoding="utf-8", timeout=1200
                                )
                            )
                            print(json.dumps({**identity, **report}), flush=True)


if __name__ == "__main__":
    main()
