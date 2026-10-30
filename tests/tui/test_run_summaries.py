from __future__ import annotations

import asyncio

import pytest

from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.providers import FauxProvider
from ouro.tui.app import OuroTuiApp
from ouro.tui.conversation.history import ConversationHistory
from tests.tui.helpers import run_ready_app
from tests.tui.test_tasks import streaming_application


class SummaryProvider:
    provider_id = "faux"

    def __init__(self, outcome):
        self.outcome = outcome
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
        yield ProviderStreamEvent(type="text_delta", text="Visible answer.")
        self.started.set()
        await self.release.wait()
        if self.outcome == "failed":
            raise ValueError("Test provider failure")
        yield ProviderStreamEvent(type="done")


def summaries(app):
    return [
        entry.run for entry in app.query_one(ConversationHistory).entries if entry.kind == "run"
    ]


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("execution", ["local", "remote"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "aborted"])
async def test_live_run_summary_matches_reloaded_history(tmp_path, backend, execution, outcome):
    provider = SummaryProvider(outcome)
    model = ModelMetadata(
        model_id="provider-model",
        provider_id="faux",
        endpoint_id="named-endpoint",
        display_name="Friendly model",
    )
    owner = streaming_application(tmp_path, provider, backend=backend, model=model)
    viewer = (
        owner
        if execution == "local"
        else streaming_application(tmp_path, FauxProvider(), backend=backend, model=model)
    )
    session = owner.runtime.sessions.create(title="Summary parity").session
    app = OuroTuiApp(application=viewer)
    await owner.start()
    try:
        async with run_ready_app(app, size=(100, 30)) as pilot:
            await app._open_session(session.session_id)
            if execution == "local":
                app._submit_run("Show the answer", "follow_up")
            else:
                await owner.supervisor.submit("Show the answer", session_id=session.session_id)
            await asyncio.wait_for(provider.started.wait(), 5)
            assert summaries(app) == []
            if outcome == "aborted":
                await owner.supervisor.abort(session.session_id, reason="Test interruption")
            else:
                provider.release.set()
            async with asyncio.timeout(6):
                while not summaries(app):
                    await pilot.pause()
            for worker in tuple(app.workers):
                if worker.group == "run":
                    await worker.wait()
            live = summaries(app)
            assert len(live) == 1
            assert live[0].status == outcome
            assert live[0].model == "Friendly model"
            assert live[0].duration_seconds is not None
            assert (
                owner.runtime.sessions.run_record(
                    session.session_id, live[0].run_id
                ).model_endpoint_id
                == "named-endpoint"
            )
            await app._open_session(session.session_id)
            restored = summaries(app)
            assert len(restored) == 1
            assert (live[0].model, live[0].duration_seconds, live[0].status, live[0].error) == (
                restored[0].model,
                restored[0].duration_seconds,
                restored[0].status,
                restored[0].error,
            )
    finally:
        provider.release.set()
        await owner.close()


async def test_observer_keeps_each_run_summary_before_the_next_runs_content(tmp_path):
    owner = streaming_application(tmp_path, FauxProvider())
    viewer = streaming_application(tmp_path, FauxProvider())
    session = owner.runtime.sessions.create(title="Two completed runs").session
    app = OuroTuiApp(application=viewer)
    await owner.start()
    try:
        async with run_ready_app(app, size=(100, 30)) as pilot:
            await app._open_session(session.session_id)
            run_ids = []
            # Let execution finish twice while rendering catches up later.
            async with app._runtime_event_apply_lock:
                for prompt in ("First answer", "Second answer"):
                    handle = await owner.supervisor.submit(prompt, session_id=session.session_id)
                    result = await owner.supervisor.wait(handle)
                    run_ids.append(result.run_id)
            async with asyncio.timeout(6):
                while len(summaries(app)) < 2:
                    await pilot.pause()
            entries = app.query_one(ConversationHistory).entries
            assert [run.run_id for run in summaries(app)] == run_ids
            for run_id in run_ids:
                positions = [index for index, entry in enumerate(entries) if entry.run_id == run_id]
                assert len(positions) >= 2
                assert entries[positions[-1]].kind == "run"
            first_summary = next(
                index
                for index, entry in enumerate(entries)
                if entry.kind == "run" and entry.run_id == run_ids[0]
            )
            second_content = next(
                index for index, entry in enumerate(entries) if entry.run_id == run_ids[1]
            )
            assert first_summary < second_content
    finally:
        await owner.close()
