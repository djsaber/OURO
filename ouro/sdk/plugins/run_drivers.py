from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .content import ToolContent
from .state import JsonValue


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, JsonValue]


@dataclass(frozen=True)
class ModelResponse:
    response_id: str
    text: str
    reasoning: str
    tool_calls: tuple[ModelToolCall, ...] = ()


@dataclass(frozen=True)
class NativeToolResult:
    tool_call_id: str
    tool_name: str
    content: ToolContent
    is_error: bool
    structured: JsonValue = None
    resource_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolBatchResult:
    suspended: bool
    results: tuple[NativeToolResult, ...] = ()


@dataclass(frozen=True)
class RunToolCall:
    name: str
    arguments: dict[str, JsonValue] = field(default_factory=dict)


class RunModels(Protocol):
    async def request(
        self, *, prompt: str = "", tools: tuple[str, ...] | None = None
    ) -> ModelResponse: ...


class RunTools(Protocol):
    async def execute(
        self, calls: tuple[RunToolCall, ...], *, checkpoint: JsonValue
    ) -> ToolBatchResult: ...

    async def execute_model_response(
        self, response_id: str, *, checkpoint: JsonValue
    ) -> ToolBatchResult: ...


@dataclass(frozen=True)
class RunInput:
    """A detached view of one committed input message, including its provenance."""

    message_id: str
    role: Literal["system", "user", "assistant", "tool_result"]
    content: tuple[dict[str, JsonValue], ...]
    scaffold: bool
    semantic_tag: str | None
    metadata: dict[str, JsonValue]
    resource_refs: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "".join(
            text
            for block in self.content
            if block.get("type") == "text" and isinstance(text := block.get("text"), str)
        )


class RunInputs(Protocol):
    async def consume(self) -> tuple[RunInput, ...]:
        """Commit pending steering at a settled decision boundary chosen by the algorithm."""
        ...


@dataclass(frozen=True)
class AgentTask:
    agent_id: str
    task: str
    description: str
    nickname: str | None = None
    execution_mode: Literal["sync", "async"] = "sync"
    resource_refs: tuple[str, ...] = ()


class RunAgents:
    """Typed Agent controls executed through the Run's native tool boundary."""

    def __init__(self, tools: RunTools) -> None:
        self._tools = tools

    async def spawn(
        self, tasks: tuple[AgentTask, ...], *, checkpoint: JsonValue
    ) -> ToolBatchResult:
        calls = []
        for task in tasks:
            arguments: dict[str, JsonValue] = {
                "agent_id": task.agent_id,
                "task": task.task,
                "description": task.description,
                "execution_mode": task.execution_mode,
                "resource_refs": list(task.resource_refs),
            }
            if task.nickname is not None:
                arguments["nickname"] = task.nickname
            calls.append(RunToolCall("SpawnAgent", arguments))
        return await self._tools.execute(tuple(calls), checkpoint=checkpoint)

    async def send(
        self,
        delegation_id: str,
        message: str,
        *,
        checkpoint: JsonValue,
        mode: Literal["steer", "follow_up"] = "steer",
    ) -> ToolBatchResult:
        return await self._tools.execute(
            (
                RunToolCall(
                    "SendAgentMessage",
                    {
                        "delegation_id": delegation_id,
                        "message": message,
                        "mode": mode,
                    },
                ),
            ),
            checkpoint=checkpoint,
        )

    async def interrupt(
        self, delegation_id: str, *, checkpoint: JsonValue, reason: str | None = None
    ) -> ToolBatchResult:
        arguments: dict[str, JsonValue] = {"delegation_id": delegation_id}
        if reason is not None:
            arguments["reason"] = reason
        return await self._tools.execute(
            (RunToolCall("InterruptAgent", arguments),), checkpoint=checkpoint
        )


class RunDriverContext(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def is_aborted(self) -> bool: ...

    @property
    def models(self) -> RunModels: ...

    @property
    def tools(self) -> RunTools: ...

    @property
    def agents(self) -> RunAgents: ...

    @property
    def inputs(self) -> RunInputs: ...


@dataclass(frozen=True)
class DriverResume:
    checkpoint: JsonValue
    results: tuple[NativeToolResult, ...] = ()


@dataclass(frozen=True)
class DriverFinished:
    text: str
    response_id: str | None = None


@dataclass(frozen=True)
class DriverSuspended:
    pass


RunDriverHandler = Callable[
    [RunDriverContext, DriverResume | None], Awaitable[DriverFinished | DriverSuspended]
]


@dataclass(frozen=True)
class RunDriverContribution:
    id: str
    handler: RunDriverHandler
