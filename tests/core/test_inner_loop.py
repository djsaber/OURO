from collections import deque

from ouro.core.inner_loop import InnerAgentLoop
from ouro.core.run_drivers import (
    DriverFinished,
    DriverModelResponse,
    DriverResume,
    DriverSuspended,
    DriverToolResult,
)
from ouro.core.tools import ToolCallRequest


class Scenario:
    def __init__(self, responses, *, suspended=False, inputs=(), completions=()):
        self.responses = deque(responses)
        self.suspended = suspended
        self.inputs = deque(inputs)
        self.completions = deque(completions)
        self.executed = []
        self.reviewed = []
        self.requests = 0
        self.consumed_inputs = 0

    async def request_model(self):
        self.requests += 1
        return self.responses.popleft()

    async def execute_model_tools(self, response_id, *, checkpoint):
        self.executed.append((response_id, checkpoint))
        return DriverToolResult(self.suspended)

    async def consume_inputs(self):
        self.consumed_inputs += 1
        return self.inputs.popleft() if self.inputs else False

    async def may_finish(self, response_id):
        self.reviewed.append(response_id)
        return self.completions.popleft() if self.completions else True


def response(identity, text="", *, tool=False):
    return DriverModelResponse(
        identity,
        text,
        "reasoning",
        (ToolCallRequest(name="Write"),) if tool else (),
    )


async def test_default_driver_returns_on_native_wait_without_requesting_another_model():
    scenario = Scenario([response("action", tool=True)], suspended=True)
    outcome = await InnerAgentLoop(turns=scenario)(scenario, None)
    assert isinstance(outcome, DriverSuspended)
    assert scenario.executed == [("action", "turn")]
    assert scenario.requests == 1
    assert scenario.consumed_inputs == 0
    assert scenario.reviewed == []


async def test_default_resume_consumes_input_and_checks_completion_without_replaying_tools():
    scenario = Scenario(
        [response("candidate", "draft"), response("final", "accepted")],
        completions=[False, True],
    )
    outcome = await InnerAgentLoop(turns=scenario)(scenario, DriverResume("turn", ()))
    assert outcome == DriverFinished("accepted", response_id="final")
    assert scenario.executed == []
    assert scenario.consumed_inputs == 3
    assert scenario.reviewed == ["candidate", "final"]


async def test_default_driver_prioritizes_inserted_input_over_completion_policy():
    scenario = Scenario(
        [response("old", "superseded"), response("new", "answer")],
        inputs=[True, False],
    )
    outcome = await InnerAgentLoop(turns=scenario)(scenario, None)
    assert outcome == DriverFinished("answer", response_id="new")
    assert scenario.reviewed == ["new"]


async def test_default_driver_continues_after_completed_native_tools():
    scenario = Scenario([response("action", tool=True), response("answer", "finished")])
    outcome = await InnerAgentLoop(turns=scenario)(scenario, None)
    assert outcome == DriverFinished("finished", response_id="answer")
    assert scenario.executed == [("action", "turn")]
    assert scenario.requests == 2
    assert scenario.consumed_inputs == 2
