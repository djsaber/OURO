import pytest

from ouro.core.content import ImageContent, TextContent
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)
from ouro.tools.result_content import limit_result_content


def test_text_budget_is_shared_across_parts_and_media_is_never_sliced():
    image = ImageContent(uri="data:image/png;base64," + "A" * 100)
    parts = [TextContent(text="x" * 60), image, TextContent(text="y" * 60)]
    bounded, truncated = limit_result_content(parts, text_limit=80, media_limit=1000)
    assert truncated
    assert image in bounded
    assert sum(len(part.text) for part in bounded if isinstance(part, TextContent)) == 80
    assert "tool output truncated" in "".join(
        part.text for part in bounded if isinstance(part, TextContent)
    )
    assert parts[-1].text == "y" * 60


def test_media_budget_is_shared_and_oversized_parts_become_explicit_text():
    image = ImageContent(uri="https://example.test/image.png")
    bounded, truncated = limit_result_content(
        [image, image], text_limit=1000, media_limit=len(image.uri)
    )
    assert truncated
    assert bounded[0] == image
    assert isinstance(bounded[1], TextContent)
    assert "omitted" in bounded[1].text


@pytest.mark.parametrize("limit", [0, 1, 20, 80])
def test_even_small_text_limits_bound_the_truncation_marker(limit):
    result, truncated = limit_result_content("x" * 100, text_limit=limit, media_limit=0)
    assert truncated and len(result) <= limit


async def test_executor_validates_and_bounds_rich_results_without_losing_structure():
    registry = ToolRegistry()
    image = ImageContent(uri="data:image/png;base64,aW1hZ2U=")

    def handler(invocation, context):
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=[TextContent(text="a" * 100), image],
        )

    registry.register(
        Tool(definition=ToolDefinition(name="Render", description="render"), handler=handler)
    )
    result = await ToolExecutor(registry).execute(
        ToolInvocation(session_id="s", run_id="r", tool_name="Render"),
        context=ToolExecutionContext(max_output_chars=70),
    )
    assert result.result is not None and not result.result.is_error
    assert result.result.metadata["output_truncated"]
    assert image in result.result.content_blocks
    assert len(result.result.text_content) <= 70
