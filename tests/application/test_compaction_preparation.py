from __future__ import annotations

import asyncio
import base64
import gc
import weakref

import pytest

from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionArtifact, CompactionRecord
from ouro.core.context_providers import (
    ContextFragment,
    ContextProviderRegistration,
    ContextProviderResult,
)
from ouro.core.input_content import InputFileReference
from ouro.core.messages import ImageContent, MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from tests.application.test_compaction import RecordingProvider, RecordingStrategy, _runtime
from tests.application.test_resources import _services
from tests.event_capture import recorded_events
from tests.session_summaries import commit_summary


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize(
    ("covered", "change"),
    [(4, "none"), (128, "none"), (4, "append"), (4, "title"), (4, "fragment")],
)
async def test_auto_compaction_reuses_views_and_reloads_only_changed_history(
    tmp_path, monkeypatch, backend, covered, change
):
    store, files, resources, _ = _services(tmp_path, backend=backend)
    session = SessionRecord()
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            role="user" if index % 2 == 0 else "assistant",
            text=f"message {index} " + "x" * 1000,
        )
        for index in range(covered + 10)
    ]
    store.create_session_snapshot(session, messages)
    commit_summary(
        store,
        CompactionRecord(
            session_id=session.session_id,
            strategy_id="test",
            trigger="manual",
            summary="earlier summary",
            covered_message_count=covered,
            covered_end_message_id=messages[covered - 1].message_id,
            history_boundary_message_id=messages[-1].message_id,
        ),
    )
    raw = b"\xff\xd8\xff" + b"media" * 8192
    source = tmp_path / "picture.jpg"
    source.write_bytes(raw)
    media = resources.prepare_input(
        MessageRecord(
            session_id=session.session_id,
            role="user",
            semantic_tag="attachments",
            input_attachments=(InputFileReference(name=source.name, path=str(source)),),
        ),
        input_modalities=frozenset({"image"}),
        workspace=str(tmp_path),
        workspace_only=True,
    )
    store.append_message(media)
    source.unlink()
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    session = store.load_session_record(session.session_id).model_copy(
        update={"active_run_id": run.run_id, "status": "running"}
    )
    store.save_session(session)
    entered, release = asyncio.Event(), asyncio.Event()

    class Strategy(RecordingStrategy):
        async def compact(self, request, *, signal=None):
            assert request.previous_summary == "earlier summary"
            assert len(request.messages) == 10 + int(change == "fragment")
            assert request.messages[0].message_id == messages[covered].message_id
            entered.set()
            await release.wait()
            return CompactionArtifact(summary="replacement summary")

    strategy = Strategy()
    runtime = _runtime(
        store, RecordingProvider(), strategy, strategy_id=strategy.strategy_id, resources=resources
    )
    runtime.adapter.model.supports_image_input = True
    preparation = runtime.context_preparation
    reads, tails, views, fragments = [], [], [], []
    load_content, load_tail = files.load_content, store.load_messages_tail
    build = preparation.context_builder.build

    def read_content(*args):
        reads.append(args[1].resource_id)
        return load_content(*args)

    def tail(session_id, *, limit=100, include_media=True):
        tails.append(limit)
        assert limit < covered + 11
        return load_tail(session_id, limit=limit, include_media=include_media)

    def track_view(**kwargs):
        view = build(**kwargs)
        views.append(weakref.ref(view.provider_context))
        return view

    def dynamic(call):
        fragments.append(call.message_count)
        return ContextProviderResult(
            fragments=[
                ContextFragment(
                    text="current dynamic value",
                    persistence="message" if change == "fragment" else "ephemeral",
                )
            ]
        )

    runtime.context_providers.replace_owner(
        "test",
        (
            ContextProviderRegistration(
                plugin_id="dev.test.compaction",
                provider_id="dynamic",
                handler=dynamic,
            ),
        ),
    )
    monkeypatch.setattr(files, "load_content", read_content)
    monkeypatch.setattr(store, "load_messages_tail", tail)
    monkeypatch.setattr(preparation.context_builder, "build", track_view)
    task = asyncio.create_task(
        preparation.prepare(
            0,
            session=session,
            run=run,
            tools=[],
            model_round=1,
            turn_id="turn_compaction",
            abort_signal=AbortSignal(),
        )
    )
    waiting = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait(
            (task, waiting), timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            await task
        assert entered.is_set(), "summary strategy was not reached"
        if change == "append":
            store.append_message(
                MessageRecord.text(
                    session_id=session.session_id, role="user", text="arrived during summary"
                )
            )
        elif change == "title":
            current = store.load_session_record(session.session_id)
            store.save_session(current.model_copy(update={"title": "renamed during summary"}))
        release.set()
        view, _ = await asyncio.wait_for(task, 10)
        assert tails == ([11, 2] if change == "append" else [11])
        # Initial Agent projection, canonical summary material, then accepted tail.
        # An actual append needs one further projection; a title change does not.
        assert len(reads) == {"append": 4, "fragment": 2}.get(change, 3)
        assert len(views) == (3 if change == "append" else 2)
        assert views[-1]() is view.provider_context
        assert fragments == [covered + 11]
        texts = [message.text_content for message in view.provider_context.messages]
        assert any("replacement summary" in text for text in texts)
        assert texts.count("current dynamic value") == 1
        assert ("arrived during summary" in texts) == (change == "append")
        actual_media = [
            block
            for message in view.provider_context.messages
            for block in message.content
            if isinstance(block, ImageContent)
        ]
        assert len(actual_media) == (0 if change == "fragment" else 1)
        if actual_media:
            assert base64.b64decode(actual_media[0].uri.partition(",")[2]) == raw
        saved = store.load_latest_context(session.session_id)
        assert saved.provider_context == view.provider_context
        assert store.load_active_compaction(
            session.session_id
        ).covered_message_count == covered + 10 + int(change == "fragment")
        # Reuse is local to this preparation; the service retains no prior payload view.
        gc.collect()
        assert all(reference() is None for reference in views[:-1])
    finally:
        release.set()
        if not task.done():
            task.cancel()
        waiting.cancel()
        await asyncio.gather(task, waiting, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_skipped_compaction_keeps_the_already_prepared_request(
    tmp_path, monkeypatch, backend
):
    store, _, _, _ = _services(tmp_path, backend=backend)
    session = SessionRecord()
    store.create_session_snapshot(
        session,
        [MessageRecord.text(session_id=session.session_id, role="user", text="x" * 4000)],
    )
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    session = store.load_session_record(session.session_id).model_copy(
        update={"active_run_id": run.run_id, "status": "running"}
    )
    store.save_session(session)
    strategy = RecordingStrategy()
    runtime = _runtime(store, RecordingProvider(), strategy, strategy_id=strategy.strategy_id)
    preparation = runtime.context_preparation
    views, tails = [], []
    build, load_tail = preparation.context_builder.build, store.load_messages_tail

    def track_view(**kwargs):
        view = build(**kwargs)
        views.append(view)
        return view

    def tail(session_id, *, limit=100, include_media=True):
        tails.append(limit)
        return load_tail(session_id, limit=limit, include_media=include_media)

    monkeypatch.setattr(preparation.context_builder, "build", track_view)
    monkeypatch.setattr(store, "load_messages_tail", tail)
    try:
        view, _ = await preparation.prepare(
            0,
            session=session,
            run=run,
            tools=[],
            model_round=1,
            turn_id="turn_no_closed_frame",
            abort_signal=AbortSignal(),
        )
        assert not strategy.requests
        assert tails == [1]
        assert len(views) == 1
        assert view.provider_context is views[0].provider_context
        assert (
            store.load_latest_context(session.session_id).provider_context == view.provider_context
        )
        assert store.load_active_compaction(session.session_id) is None
        assert any(
            event.event_type == "compaction_skipped"
            for event in recorded_events(session.session_id)
        )
    finally:
        await runtime.close()
