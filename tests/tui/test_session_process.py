from __future__ import annotations

import asyncio
import multiprocessing
from multiprocessing.connection import Connection

import pytest

from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from tests.tui.helpers import run_ready_app, wait_until
from tests.tui.test_background_navigation import NavigationProvider, submit
from tests.tui.test_tasks import streaming_application


async def receive(connection: Connection):
    assert await asyncio.to_thread(connection.poll, 30), "peer did not reach its next boundary"
    return connection.recv()


async def request(connection: Connection, command):
    connection.send(command)
    return await receive(connection)


def observe_in_process(home, backend, session_id, connection):
    try:
        asyncio.run(observe(home, backend, session_id, connection))
    finally:
        connection.close()


async def observe(home, backend, session_id, connection):
    application = streaming_application(home, None, backend=backend)
    app = OuroTuiApp(application=application, close_application=False)
    held, release = asyncio.Event(), asyncio.Event()
    release.set()
    original = app._apply_runtime_event_batch

    async def hold_body(events, *, input_messages):
        if not release.is_set():
            held.set()
            await release.wait()
        await original(events, input_messages=input_messages)

    app._apply_runtime_event_batch = hold_body
    try:
        async with run_ready_app(app, size=(100, 32)) as pilot:
            await app._open_session(session_id)
            connection.send("ready")
            while True:
                command = await receive(connection)
                if command == "close":
                    break
                if command == "hold":
                    release.clear()
                    connection.send(app._observation_subscription.applied_sequence)
                elif command == "held":
                    await asyncio.wait_for(held.wait(), 10)
                    await pilot.press("v")
                    connection.send(
                        (
                            app._observation_subscription.applied_sequence,
                            len(app._pending_runtime_events),
                            app.query_one(Composer).text,
                        )
                    )
                elif isinstance(command, tuple) and command[0] == "catch_up":
                    release.set()
                    await wait_until(
                        lambda sequence=command[1], text=command[2]: (
                            app._observation_subscription.applied_sequence >= sequence
                            and app._projection.assistant_text == text
                        ),
                        pilot,
                        description="remote observer caught up",
                    )
                    await app.query_one(ConversationHistory).wait_until_rendered()
                    connection.send(
                        (
                            app._projection.assistant_text,
                            [
                                entry.run.run_id
                                for entry in app.query_one(ConversationHistory).entries
                                if entry.run is not None
                            ],
                        )
                    )
                else:
                    raise AssertionError(f"unexpected peer command: {command}")
            assert not application.supervisor._jobs_by_session
        await asyncio.gather(*application.supervisor._event_stream_tasks)
        assert not application.supervisor._event_streams
        assert not application.supervisor._subscriptions
        connection.send("closed")
    finally:
        release.set()
        await application.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_two_process_tuis_keep_execution_and_slow_observation_independent(tmp_path, backend):
    provider = NavigationProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    app = OuroTuiApp(application=application, close_application=False)
    process = None
    reader = None
    try:
        async with run_ready_app(app, size=(100, 32)) as pilot:
            sessions, workers = [], []
            for prompt in ("Alpha", "Beta", "Gamma"):
                if sessions:
                    await app._new_session()
                identity, worker = await submit(app, pilot, provider, prompt)
                sessions.append(identity)
                workers.append(worker)
            context = multiprocessing.get_context("spawn")
            reader, child = context.Pipe()
            process = context.Process(
                target=observe_in_process, args=(tmp_path, backend, sessions[0], child)
            )
            process.start()
            child.close()
            assert await receive(reader) == "ready"
            before = await request(reader, "hold")
            provider.text("Alpha", "held boundary, ")
            applied, pending, draft = await request(reader, "held")
            assert applied == before and pending <= 128 and draft == "v"
            provider.text("Beta", "Background progress. ")
            provider.text("Gamma", "Independent progress. ")
            committed = await application.supervisor.subscribe(sessions[0])
            try:
                async with asyncio.timeout(60):
                    for index in range(129):
                        marker = f"piece {index}, "
                        provider.text("Alpha", marker)
                        # Await each current Draft marker after adapter coalescing
                        # before producing the next to cross the 128-event buffer
                        # deterministically, regardless of timer granularity/load.
                        while True:
                            notification = await committed.get()
                            assert notification.diagnostic is None, notification.diagnostic
                            event = notification.event
                            if (
                                event is not None
                                and event.event_type == "model_text_delta"
                                and marker in (event.text or "")
                            ):
                                break
                        if index in (32, 64, 96):
                            await app._open_session(sessions[index // 32 - 1])
            finally:
                committed.close()
            await app._open_session(sessions[0])
            await wait_until(
                lambda: "piece 128," in app._projection.assistant_text,
                pilot,
                description="owner continues while remote observer is held",
            )
            events = application.runtime.store.load_session(sessions[0]).events
            assert not any(event.event_type == "model_text_delta" for event in events)
            latest = application.runtime.store.load_latest_request(sessions[0])
            assert latest is not None
            header, _ = latest
            response = application.runtime.store.load_response_draft(
                sessions[0], header.context_id, limit=1000
            )
            assert response is not None and len(response.chunks) > 128
            applied, pending, draft = await request(reader, "held")
            assert applied == before and pending <= 128 and draft == "vv"
            text, summaries = await request(
                reader, ("catch_up", events[-1].sequence, app._projection.assistant_text)
            )
            assert text == app._projection.assistant_text
            assert summaries == []
            provider.finish("Alpha")
            await asyncio.wait_for(workers[0].wait(), 15)
            run_ids = []
            for follow_up in (False, True):
                if follow_up:
                    _, worker = await submit(app, pilot, provider, "Follow")
                    provider.text("Follow", "Second shared answer. ")
                    provider.finish("Follow")
                    await asyncio.wait_for(worker.wait(), 15)
                snapshot = application.runtime.store.load_session(sessions[0])
                run_ids.append(snapshot.runs[-1].run_id)
                text, summaries = await request(
                    reader,
                    ("catch_up", snapshot.events[-1].sequence, snapshot.messages[-1].text_content),
                )
                assert text == snapshot.messages[-1].text_content
                assert summaries == run_ids
            assert await request(reader, "close") == "closed"
            await asyncio.to_thread(process.join, 10)
            assert process.exitcode == 0
            assert application.supervisor.status(sessions[1]).status == "running"
            assert application.supervisor.status(sessions[2]).status == "running"
            await application.supervisor.abort(sessions[1])
            await asyncio.wait_for(workers[1].wait(), 15)
            provider.finish("Gamma")
            await asyncio.wait_for(workers[2].wait(), 15)
            assert application.runtime.store.load_session(sessions[1]).runs[-1].status == "aborted"
            assert (
                application.runtime.store.load_session(sessions[2]).runs[-1].status == "completed"
            )
            assert not application.supervisor._jobs_by_session
    finally:
        if process is not None:
            if process.is_alive():
                reader.send("close")
                await asyncio.to_thread(process.join, 5)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
            process.close()
        if reader is not None:
            reader.close()
        for prompt in provider.streams:
            provider.finish(prompt)
        await application.close()
