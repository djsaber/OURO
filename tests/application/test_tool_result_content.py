from copy import deepcopy

import pytest

from ouro.core.content import ImageContent, TextContent
from ouro.core.provider import ProviderStreamEvent
from ouro.core.tools import ToolCallRequest
from ouro.providers import FauxProvider
from ouro.tools import Tool, ToolDefinition, ToolResult
from tests.providers.test_tool_result_content import URI, encode
from tests.tui.test_history_commands import application_at


class RecordingProvider(FauxProvider):
    def __init__(self, protocol, *, request_tools=True):
        batches = [
            [
                ProviderStreamEvent.from_tool_call(
                    ToolCallRequest(id=f"call-{i}", name="Render", source_index=i)
                )
                for i in range(2)
            ]
            + [ProviderStreamEvent(type="done")],
            [
                ProviderStreamEvent(type="text_delta", text="done"),
                ProviderStreamEvent(type="done"),
            ],
        ]
        super().__init__(event_batches=batches if request_tools else None)
        self.protocol = protocol
        self.requests = []

    async def stream(self, *, model, messages, **kwargs):
        self.requests.append(
            encode(
                self.protocol,
                deepcopy(messages),
                image_input=model.supports_image_input,
                audio_input=model.supports_audio_input,
                video_input=model.supports_video_input,
            )
        )
        async for event in super().stream(model=model, messages=messages, **kwargs):
            yield event


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("image_input", [False, True])
async def test_rich_results_survive_execution_storage_replay_and_route_projection(
    tmp_path, backend, protocol, image_input
):
    provider = RecordingProvider(protocol)
    app = application_at(tmp_path, provider, backend=backend)
    app.runtime.adapter.model.supports_image_input = image_input
    executed = []

    def render(invocation, context):
        executed.append(invocation.tool_call_id)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=[TextContent(text="rendered"), ImageContent(uri=URI)],
        )

    app.runtime.tools.register(
        Tool(definition=ToolDefinition(name="Render", description="render"), handler=render)
    )
    result = await app.runtime.run("render")
    assert result.run.status == "completed"
    assert executed == ["call-0", "call-1"]
    assert (URI.partition(",")[2] in str(provider.requests[-1])) == image_input
    if not image_input:
        assert str(provider.requests[-1]).count("This model cannot view images.") == 2
    if protocol == "chat" and image_input:
        roles = [item["role"] for item in provider.requests[-1]]
        start = roles.index("tool")
        assert roles[start : start + 4] == ["tool", "tool", "user", "user"]

    snapshot = app.runtime.store.load_runtime_session(result.session.session_id)
    outputs = [message for message in snapshot.messages if message.role == "tool_result"]
    assert len(outputs) == 2
    assert all(
        any(isinstance(part, ImageContent) for part in message.content) for message in outputs
    )
    assert not any(message.scaffold and message.role == "user" for message in snapshot.messages)
    await app.close()

    replay_provider = RecordingProvider("responses", request_tools=False)
    reopened = application_at(tmp_path, replay_provider, backend=backend)
    try:
        # Persisted content remains independent of the route that consumed it.
        reopened.runtime.adapter.model.supports_image_input = True
        lifecycle = reopened.runtime._tool_calls_for_run(result.run.run_id).runs
        replay = lifecycle.append_tool_result_messages(
            session=result.session,
            run=result.run,
            results=result.tool_results,
            first_sequence=100,
            turn_id=outputs[0].turn_id,
        )
        assert [message.message_id for message in replay] == [
            message.message_id for message in outputs
        ]
        assert (
            reopened.runtime.store.load_runtime_session(result.session.session_id).messages
            == snapshot.messages
        )
        await reopened.runtime.run("continue", session_id=result.session.session_id)
        assert URI in str(replay_provider.requests[-1])
    finally:
        await reopened.close()
