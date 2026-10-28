import base64

import pytest

import ouro.application.inputs.files as file_inputs
from ouro.core.content import ImageContent
from ouro.core.provider import ProviderStreamEvent
from ouro.core.tools import ToolCallRequest
from ouro.providers import FauxProvider
from ouro.tools import builtin_tools
from tests.application.test_compaction import RecordingStrategy
from tests.providers.test_tool_result_content import encode
from tests.run_facts import run_tool_results
from tests.tools.test_builtin_view_image import image_file
from tests.tui.test_history_commands import application_at


class ImageLoopProvider(FauxProvider):
    def __init__(self, path, rounds=6):
        super().__init__(
            event_batches=[
                [
                    ProviderStreamEvent(type="text_delta", text=f"Observation {index}"),
                    ProviderStreamEvent.from_tool_call(
                        ToolCallRequest(
                            id=f"image-{index}", name="ViewImage", arguments={"path": str(path)}
                        )
                    ),
                    ProviderStreamEvent(type="done"),
                ]
                for index in range(rounds)
            ]
            + [
                [
                    ProviderStreamEvent(type="text_delta", text="finished"),
                    ProviderStreamEvent(type="done"),
                ]
            ]
        )
        self.requests = []

    async def stream(self, *, model, messages, **kwargs):
        self.requests.append(list(messages))
        async for event in super().stream(model=model, messages=messages, **kwargs):
            yield event

    def _next_events(self):
        if self._stream_count >= len(self._event_batches):
            return self._events
        return super()._next_events()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_image_loop_releases_headroom_then_preserves_protocol_prefixes(
    tmp_path, monkeypatch, backend
):
    path = image_file(tmp_path / "image.png")
    size = len(base64.b64encode(path.read_bytes()))
    monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", size * 3)
    provider = ImageLoopProvider(path)
    app = application_at(tmp_path / "home", provider, backend=backend)
    app.runtime.adapter.model.supports_image_input = True
    for tool in builtin_tools():
        app.runtime.tools.register(tool)
    try:
        result = await app.runtime.run("Inspect repeatedly", workspace=tmp_path)
        assert result.run.status == "completed", result.run.error
        counts = [
            sum(isinstance(part, ImageContent) for message in request for part in message.content)
            for request in provider.requests
        ]
        assert counts == [0, 1, 2, 3, 1, 2, 3]
        for protocol in ("chat", "responses", "messages"):
            wires = [encode(protocol, request, image_input=True) for request in provider.requests]
            for index in (1, 2, 3, 5, 6):
                assert wires[index][: len(wires[index - 1])] == wires[index - 1]
        canonical = app.runtime.store.load_session(result.session.session_id).messages
        assert sum(isinstance(part, ImageContent) for m in canonical for part in m.content) == 6
        assert all(f"Observation {i}" in str(provider.requests[-1]) for i in range(6))
        saved = app.runtime.store.load_latest_context(result.session.session_id)
        state = app.runtime.store.load_session_record(result.session.session_id).context_selection
        assert state.media_elided_through is not None
        assert "media_projection" not in saved.context_metadata
        await app.close()
        path.unlink()
        replay = ImageLoopProvider(path, rounds=0)
        app = application_at(tmp_path / "home", replay, backend=backend)
        app.runtime.adapter.model.supports_image_input = True
        for tool in builtin_tools():
            app.runtime.tools.register(tool)
        strategy = RecordingStrategy()
        app.runtime.compaction_strategies.register(strategy)
        app.runtime.compaction_config.strategy_id = strategy.strategy_id
        continued = await app.runtime.run("Continue", session_id=result.session.session_id)
        assert continued.run.status == "completed", continued.run.error
        for protocol in ("chat", "responses", "messages"):
            old = encode(protocol, provider.requests[-1], image_input=True)
            new = encode(protocol, replay.requests[-1], image_input=True)
            assert new[: len(old)] == old

        branch = app.runtime.sessions.branch(result.session.session_id, canonical[-1].message_id)
        branch_record = app.runtime.store.load_session_record(branch.session.session_id)
        assert (
            branch_record.context_selection.media_elided_through.sequence
            == state.media_elided_through.sequence
        )
        # A policy can deliberately restore media, independently of the last request.
        app.runtime.sessions.set_context_selection(
            branch.session.session_id,
            branch_record.context_selection.model_copy(update={"media_elided_through": None}),
        )
        monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", size * 10)
        branched = await app.runtime.run("Review", session_id=branch.session.session_id)
        assert branched.run.status == "completed", branched.run.error
        assert sum(isinstance(p, ImageContent) for m in replay.requests[-1] for p in m.content) == 6
        monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", size * 3)

        # Each forced summary covers one oldest frame. The third covers the media boundary.
        for _ in range(3):
            before = app.runtime.store.load_latest_context(result.session.session_id)
            compacted = await app.runtime.compact(session_id=result.session.session_id)
            assert compacted.compaction is not None
            assert app.runtime.store.load_latest_context(result.session.session_id) == before
            continued = await app.runtime.run("Continue", session_id=result.session.session_id)
            assert continued.run.status == "completed", continued.run.error
            current = app.runtime.store.load_session_record(result.session.session_id)
            assert current.context_selection.media_elided_through == state.media_elided_through
            assert (
                sum(isinstance(p, ImageContent) for m in replay.requests[-1] for p in m.content)
                == 3
            )
    finally:
        await app.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_oversized_new_tool_batch_fails_before_provider_and_keeps_saved_boundary(
    tmp_path,
    monkeypatch,
    backend,
):
    path = image_file(tmp_path / "image.png")
    size = len(base64.b64encode(path.read_bytes()))
    monkeypatch.setattr(file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", size * 3)
    provider = ImageLoopProvider(path, rounds=4)
    app = application_at(tmp_path / "home", provider, backend=backend)
    app.runtime.adapter.model.supports_image_input = True
    for tool in builtin_tools():
        app.runtime.tools.register(tool)
    try:
        first = await app.runtime.run("Inspect", workspace=tmp_path)
        selection = app.runtime.store.load_session_record(
            first.session.session_id
        ).context_selection
        provider._event_batches = [
            [
                ProviderStreamEvent.from_tool_call(
                    ToolCallRequest(
                        id=f"batch-{i}",
                        name="ViewImage",
                        arguments={"path": str(path)},
                        source_index=i,
                    )
                )
                for i in range(4)
            ]
            + [ProviderStreamEvent(type="done")]
        ]
        provider._stream_count = 0
        seen = len(provider.requests)
        result = await app.runtime.run("Read a batch", session_id=first.session.session_id)
        assert result.run.status == "failed"
        assert "request media limit" in result.run.error
        assert len(provider.requests) == seen + 1
        assert len(run_tool_results(app.runtime.store, result)) == 4
        latest = app.runtime.store.load_latest_context(first.session.session_id)
        assert (
            app.runtime.store.load_session_record(first.session.session_id).context_selection
            == selection
        )
        assert not any(
            m.tool_result and m.tool_result.tool_call_id == "batch-0"
            for m in latest.provider_context.messages
        )
    finally:
        await app.close()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_failed_provider_request_retries_the_committed_media_projection(
    tmp_path, monkeypatch, backend
):
    path = image_file(tmp_path / "image.png")
    monkeypatch.setattr(
        file_inputs, "DEFAULT_REQUEST_MEDIA_CHARS", len(base64.b64encode(path.read_bytes())) * 3
    )
    provider = ImageLoopProvider(path, rounds=4)
    provider._event_batches[-1] = [ProviderStreamEvent(type="error", error="temporary failure")]
    app = application_at(tmp_path / "home", provider, backend=backend)
    app.runtime.adapter.model.supports_image_input = True
    for tool in builtin_tools():
        app.runtime.tools.register(tool)
    try:
        first = await app.runtime.run("Inspect", workspace=tmp_path)
        assert first.run.status == "failed"
        selection = app.runtime.store.load_session_record(
            first.session.session_id
        ).context_selection
        retried = await app.runtime.continue_run(session_id=first.session.session_id)
        assert retried.run.status == "completed", retried.run.error
        for protocol in ("chat", "responses", "messages"):
            old = encode(protocol, provider.requests[-2], image_input=True)
            new = encode(protocol, provider.requests[-1], image_input=True)
            assert new[: len(old)] == old
        assert (
            app.runtime.store.load_session_record(first.session.session_id).context_selection
            == selection
        )
    finally:
        await app.close()
