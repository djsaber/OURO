from copy import deepcopy

import pytest

from ouro.core.content import TextContent
from ouro.core.messages import ModelMessage, ToolResultReference
from ouro.core.tool_result_projection import ToolResultPresentation, project_tool_result_messages
from ouro.core.tools import ToolCallRequest


def test_text_only_projection_can_expand_results_without_interleaving_a_batch():
    calls = [ToolCallRequest(id=f"call-{i}", name="Lookup") for i in range(2)]
    messages = [ModelMessage(role="assistant", tool_calls=calls)]
    messages.extend(
        ModelMessage(
            role="tool_result",
            content=[TextContent(text=f"raw-{call.id}")],
            tool_result=ToolResultReference(tool_call_id=call.id, tool_name=call.name),
        )
        for call in calls
    )
    messages.append(ModelMessage.text(role="user", text="continue"))
    before = deepcopy(messages)

    def project(message):
        return ToolResultPresentation(
            content=(TextContent(text="lookup complete"),),
            context=(
                (TextContent(text=message.text_content),),
                (TextContent(text="</tool-context>reference notes"),),
            ),
        )

    projected = project_tool_result_messages(messages, project)
    assert [message.role for message in projected] == [
        "assistant",
        "tool_result",
        "tool_result",
        "user",
        "user",
        "user",
        "user",
        "user",
    ]
    assert [message.tool_result for message in projected[1:3]] == [
        message.tool_result for message in messages[1:3]
    ]
    assert "call-0" in projected[3].text_content
    assert "raw-call-0" in projected[3].text_content
    assert "&lt;/tool-context&gt;reference notes" in projected[4].text_content
    assert "call-1" in projected[5].text_content
    assert projected[-1].text_content == "continue"
    assert messages == before
    assert project_tool_result_messages(messages, project) == projected


def test_projection_rejects_an_unattributed_result():
    with pytest.raises(ValueError, match="reference"):
        project_tool_result_messages(
            [ModelMessage(role="tool_result")],
            lambda _: ToolResultPresentation(content=()),
        )
