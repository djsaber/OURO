from unittest.mock import patch

import pytest

from ouro.core.interactions import AnswerResolution
from ouro.tools import Tool, ToolDefinition, ToolExecutionContext, ToolInputRequest, ToolInvocation


async def test_tool_execution_and_resume_use_the_bound_signature_without_reflection():
    seen = []
    request = ToolInputRequest(data={"type": "question"})

    async def execute(invocation, context):
        seen.append((invocation, context))
        return request

    async def resume(invocation, resolution, request, context):
        seen.append((invocation, resolution, request, context))
        return request

    tool = Tool(
        definition=ToolDefinition(name="Question", description="Question", resumable=True),
        handler=execute,
        resume_handler=resume,
    )
    invocation = ToolInvocation(tool_name="Question", session_id="session", run_id="run")
    context = ToolExecutionContext()
    resolution = AnswerResolution(value="answer")
    with patch("ouro.tools.base.signature", side_effect=AssertionError("execution inspected")):
        assert await tool.execute(invocation, context=context) is request
        assert (
            await tool.resume(invocation, resolution, request=request, context=context) is request
        )
    assert seen == [(invocation, context), (invocation, resolution, request, context)]


@pytest.mark.parametrize("field", ["handler", "resume_handler"])
def test_invalid_internal_callback_signature_is_rejected_when_tool_is_built(field):
    arguments = {"handler": lambda invocation, context: None, field: lambda invocation: None}
    with pytest.raises(ValueError, match="signature"):
        Tool(definition=ToolDefinition(name="Invalid", description="Invalid"), **arguments)
