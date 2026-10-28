import pytest

from ouro.core.content import ImageContent
from ouro.core.provider import ProviderStreamEvent
from ouro.core.tools import ToolCallRequest
from ouro.providers import FauxProvider
from ouro.tools import builtin_tools
from tests.providers.test_tool_result_content import encode
from tests.run_facts import run_tool_results
from tests.tools.test_builtin_view_image import image_file
from tests.tui.test_history_commands import application_at


class ImageProvider(FauxProvider):
    def __init__(self, protocol, path):
        super().__init__(
            event_batches=[
                [
                    ProviderStreamEvent.from_tool_call(
                        ToolCallRequest(
                            id=f"image-{i}",
                            name="ViewImage",
                            arguments={"path": str(path)},
                            source_index=i,
                        )
                    )
                    for i in range(2)
                ]
                + [ProviderStreamEvent(type="done")],
                [
                    ProviderStreamEvent(type="text_delta", text="inspected"),
                    ProviderStreamEvent(type="done"),
                ],
            ]
        )
        self.protocol = protocol
        self.requests = []

    async def stream(self, *, model, messages, **kwargs):
        self.requests.append(
            encode(self.protocol, messages, image_input=model.supports_image_input)
        )
        async for event in super().stream(model=model, messages=messages, **kwargs):
            yield event


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("protocol", ["chat", "responses", "messages"])
@pytest.mark.parametrize("image_input", [False, True])
async def test_builtin_view_image_reaches_model_through_normal_execution_and_projection(
    tmp_path, backend, protocol, image_input
):
    path = image_file(tmp_path / "screen.png")
    provider = ImageProvider(protocol, path)
    app = application_at(tmp_path, provider, backend=backend)
    app.runtime.adapter.model.supports_image_input = image_input
    for tool in builtin_tools():
        app.runtime.tools.register(tool)
    try:
        result = await app.runtime.run("inspect the screenshot", workspace=tmp_path)
        assert result.run.status == "completed"
        assert len(run_tool_results(app.runtime.store, result)) == 2
        assert all(not item.is_error for item in run_tool_results(app.runtime.store, result)), (
            run_tool_results(app.runtime.store, result)
        )
        snapshot = app.runtime.store.load_session(result.session.session_id)
        results = [message for message in snapshot.messages if message.role == "tool_result"]
        assert [message.tool_result.tool_call_id for message in results] == ["image-0", "image-1"]
        assert not any(message.role == "user" and message.scaffold for message in snapshot.messages)
        image = next(part for part in results[0].content if isinstance(part, ImageContent))
        assert (image.uri.partition(",")[2] in str(provider.requests[-1])) == image_input
        if not image_input:
            assert str(provider.requests[-1]).count("This model cannot view images.") == 2
        if protocol == "chat" and image_input:
            roles = [message["role"] for message in provider.requests[-1]]
            start = roles.index("tool")
            assert roles[start : start + 4] == ["tool", "tool", "user", "user"]
    finally:
        await app.close()
