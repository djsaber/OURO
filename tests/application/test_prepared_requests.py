from __future__ import annotations

import pytest

import ouro.core.context as contexts
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.context_providers import (
    ContextFragment,
    ContextProviderRegistration,
    ContextProviderRegistry,
    ContextProviderResult,
)
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommit
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.run_facts import run_events


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_estimation_record_send_and_query_share_prepared_provider_values(
    tmp_path, monkeypatch, backend
):
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    prepared = []
    dynamic_calls = []
    estimated_messages = []
    estimate = contexts._estimate_visible_message_tokens

    def track_estimate(message, estimator):
        estimated_messages.append(message.text_content)
        return estimate(message, estimator)

    monkeypatch.setattr(contexts, "_estimate_visible_message_tokens", track_estimate)
    providers = ContextProviderRegistry()

    def dynamic(call):
        dynamic_calls.append(call)
        return ContextProviderResult(
            fragments=[
                ContextFragment(
                    text=f"facts {len(dynamic_calls)} </runtime-facts>",
                    semantic_tag="runtime-facts",
                )
            ]
        )

    providers.replace_owner(
        "facts",
        (
            ContextProviderRegistration(
                plugin_id="dev.test.context",
                provider_id="facts",
                handler=dynamic,
            ),
        ),
    )

    class CapturingProvider(FauxProvider):
        async def stream(self, **kwargs):
            # Identity proves this is the estimated/prepared value, not a second
            # equivalent projection performed at the Adapter boundary.
            value = prepared[-1].provider_context
            assert kwargs["messages"] is value.messages
            assert kwargs["tools"] is value.tools
            assert kwargs["system_prompt"] == value.system_prompt
            async for event in super().stream(**kwargs):
                yield event

    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            CapturingProvider(), ModelMetadata(model_id="test", provider_id="test")
        ),
        tools=ToolRegistry(),
        context_providers=providers,
    )
    build = runtime.context_preparation.context_builder.build
    project = contexts.model_visible_message

    def forbidden(*args, **kwargs):
        pytest.fail("prepared input was projected again")

    def prepare_once(**kwargs):
        view = build(**kwargs)
        prepared.append(view)
        monkeypatch.setattr(contexts, "model_visible_message", forbidden)
        return view

    monkeypatch.setattr(runtime.context_preparation.context_builder, "build", prepare_once)
    try:
        first = await runtime.run("first request")
        assert first.run.status == "completed"
        first_record = store.load_latest_context(first.session.session_id)
        assert first_record.response_message_id is not None
        assert (
            store.load_message(first.session.session_id, first_record.response_message_id).turn_id
            == first_record.turn_id
        )
        _, saved_input = store.load_latest_request(first.session.session_id)
        assert saved_input.manifest.referenced_message_ids
        assert "first request" not in saved_input.model_dump_json()
        assert first_record.provider_context == prepared[0].provider_context
        assert first_record.provider_context.messages[0].text_content == (
            "<runtime-facts>\nfacts 1 &lt;/runtime-facts&gt;\n</runtime-facts>"
        )
        assert (
            runtime.sessions.context(first.session.session_id).provider_context
            == first_record.provider_context
        )
        assert len(dynamic_calls) == len(prepared) == 1
        monkeypatch.setattr(contexts, "model_visible_message", project)
        second = await runtime.run("second request", session_id=first.session.session_id)
        assert second.run.status == "completed"
        assert len(dynamic_calls) == len(prepared) == 2
        current = runtime.sessions.context(first.session.session_id)
        assert current.provider_context == prepared[1].provider_context
        assert "facts 2" in current.provider_context.messages[0].text_content
        assert "facts 1" in first_record.provider_context.messages[0].text_content
        assert estimated_messages.count("first request") == 1
        assert estimated_messages.count("second request") == 1
    finally:
        await runtime.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("after_prepare", ["interrupt", "abort"])
async def test_preparation_commit_does_not_claim_dispatch_and_abort_prevents_provider(
    tmp_path, monkeypatch, backend, after_prepare
):
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()

    class ForbiddenProvider(FauxProvider):
        async def stream(self, **kwargs):
            pytest.fail("provider was entered after preparation lost its owner")
            yield  # pragma: no cover

    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            ForbiddenProvider(), ModelMetadata(model_id="test", provider_id="test")
        ),
        tools=ToolRegistry(),
    )
    observed = []

    async def after_commit(*, session, run, view, model_round):
        current = store.load_session_record(session.session_id)
        saved_run = store.load_run(session.session_id, run.run_id)
        header = store.load_latest_context_header(session.session_id)
        assert header.context_id == saved_run.current_request_id == view.context_id
        assert header.started_at is None
        assert saved_run.model_round_count == 0
        observed.append(header)
        if after_prepare == "interrupt":
            raise RuntimeError("injected observer interruption")
        store.commit_session(
            SessionCommit(
                before=current,
                after=current.model_copy(update={"active_run_id": None, "status": "idle"}),
                runs=(saved_run.model_copy(update={"abort_requested": True, "status": "aborted"}),),
            )
        )

    monkeypatch.setattr(runtime.context_preparation, "_observe_built_context", after_commit)
    try:
        result = await runtime.run("prepare once")
        assert len(observed) == 1
        latest = store.load_latest_context_header(result.session.session_id)
        assert latest.started_at is None
        saved_run = store.load_run(result.session.session_id, result.run.run_id)
        assert saved_run.current_request_id == latest.context_id
        assert saved_run.model_round_count == 0
        assert not any(
            event.event_type == "model_request_started" for event in run_events(store, result)
        )
    finally:
        await runtime.close()
