"""Controlled providers for terminal application integration tests."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from ouro.core.messages import ModelMessage
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderRetryNotice,
    ProviderStreamEvent,
)
from ouro.core.tools import ToolCallRequest
from ouro.mcp import MCPRemoteTool, MCPRemoteToolResult, MCPServerCapabilities, MCPServerInfo


class BlockingStreamProvider:
    provider_id = "blocking"

    def __init__(self) -> None:
        self.first_chunk_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="text_delta", text="first ")
        self.first_chunk_sent.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="text_delta", text="second")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class RetryThenBlockProvider:
    provider_id = "retry-then-block"

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(
            type="retry_scheduled",
            retry=ProviderRetryNotice(
                attempt=3,
                max_attempts=5,
                delay_seconds=0,
                reason="temporary transport failure",
            ),
        )
        await self.release.wait()
        yield ProviderStreamEvent(type="text_delta", text="Recovered.")
        yield ProviderStreamEvent(type="done")


class BlockingReasoningProvider:
    provider_id = "blocking-reasoning"

    def __init__(self) -> None:
        self.first_chunk_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="Inspecting ")
        self.first_chunk_sent.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="the event path.")
        yield ProviderStreamEvent(type="text_delta", text="Done.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class ReasoningBurstThenBlockProvider:
    provider_id = "reasoning-burst"

    def __init__(self, *, reasoning_chunks: int = 270) -> None:
        self.reasoning_chunks = reasoning_chunks
        self.visible_chunk_sent = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages, tools
        for _ in range(self.reasoning_chunks):
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="r")
        yield ProviderStreamEvent(type="text_delta", text="visible while running")
        self.visible_chunk_sent.set()
        await self.release.wait()
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class LiveMcpSession:
    def __init__(self) -> None:
        self.initialize_gate = asyncio.Event()
        self.tools = [self._tool("search")]

    async def initialize(self) -> MCPServerInfo:
        await self.initialize_gate.wait()
        return MCPServerInfo(
            name="Live MCP",
            version="1.0",
            protocol_version="2025-11-25",
            capabilities=MCPServerCapabilities(tools=True, tools_list_changed=True),
        )

    async def list_tools(
        self,
        cursor: str | None = None,
    ) -> tuple[list[MCPRemoteTool], str | None]:
        del cursor
        return list(self.tools), None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> MCPRemoteToolResult:
        del name, arguments
        return MCPRemoteToolResult(content="ok")

    async def ping(self) -> None:
        return None

    @staticmethod
    def _tool(name: str) -> MCPRemoteTool:
        return MCPRemoteTool(
            name=name,
            title=name.title(),
            description=f"{name.title()} through MCP.",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True},
        )


class LiveMcpFactory:
    def __init__(self, session: LiveMcpSession) -> None:
        self.session = session
        self.tools_changed = lambda: None
        self.connection_lost = lambda _error: None

    @asynccontextmanager
    async def open(
        self,
        config,
        *,
        resolve_credential,
        diagnostic,
        tools_changed,
        connection_lost,
    ):
        del config, resolve_credential, diagnostic
        self.tools_changed = tools_changed
        self.connection_lost = connection_lost
        yield self.session


class ToolCapturingProvider:
    provider_id = "tool-capturing"

    def __init__(self) -> None:
        self.tool_batches: list[list[dict[str, object]]] = []

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages
        self.tool_batches.append(list(tools or []))
        yield ProviderStreamEvent(type="text_delta", text="Tools inspected.")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class QueuedSubagentApprovalProvider:
    """Keep a child active long enough to queue input, then request approval."""

    provider_id = "queued-subagent-approval"

    def __init__(self) -> None:
        self.child_started = asyncio.Event()
        self.release_child = asyncio.Event()
        self.queued_input_consumed = asyncio.Event()
        self.root_round = 0
        self.child_round = 0

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, messages
        names = [str(item["name"]) for item in (tools or [])]
        if "SpawnAgent" not in names:
            if self.child_round == 0:
                self.child_round += 1
                self.child_started.set()
                await self.release_child.wait()
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": "child_approval_call",
                        "name": "Danger",
                        "arguments": {},
                    }
                )
                yield ProviderStreamEvent(type="done")
                return
            yield ProviderStreamEvent(type="text_delta", text="child completed")
            yield ProviderStreamEvent(type="done")
            return

        if self.root_round == 0:
            self.root_round += 1
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "spawn_child_call",
                    "name": "SpawnAgent",
                    "arguments": {
                        "agent_id": "general",
                        "task": "Run the Danger test tool and report completion.",
                        "description": "TUI child approval test",
                        "execution_mode": "sync",
                    },
                }
            )
            yield ProviderStreamEvent(type="done")
            return
        if self.root_round == 1:
            self.root_round += 1
            yield ProviderStreamEvent(type="text_delta", text="parent resumed")
            yield ProviderStreamEvent(type="done")
            return
        self.queued_input_consumed.set()
        yield ProviderStreamEvent(type="text_delta", text="queued input consumed")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class MultiChildStreamingProvider:
    """Run two async child Sessions concurrently while the root completes."""

    provider_id = "multi-child-streaming"

    def __init__(self) -> None:
        self.root_round = 0
        self.root_prefix = "\n\n".join(f"Root history paragraph {index}" for index in range(80))
        self.child_prefixes = {
            task: "\n\n".join(f"{label} child paragraph {index}" for index in range(30))
            for task, label in (
                ("first child task", "first"),
                ("second child task", "second"),
            )
        }
        self.root_stream_started = asyncio.Event()
        self.release_root = asyncio.Event()
        self.first_chunks = {
            "first child task": asyncio.Event(),
            "second child task": asyncio.Event(),
        }
        self.releases = {
            "first child task": asyncio.Event(),
            "second child task": asyncio.Event(),
        }

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt
        names = [str(item["name"]) for item in (tools or [])]
        if "SpawnAgent" in names:
            if self.root_round == 0:
                self.root_round += 1
                for index, task in enumerate(self.first_chunks, start=1):
                    yield ProviderStreamEvent.from_tool_call(
                        {
                            "id": f"spawn_child_{index}",
                            "name": "SpawnAgent",
                            "arguments": {
                                "agent_id": "general",
                                "task": task,
                                "description": f"Observe child {index}",
                                "execution_mode": "async",
                            },
                        },
                        index=index - 1,
                    )
                yield ProviderStreamEvent(type="done")
                return
            yield ProviderStreamEvent(type="text_delta", text=self.root_prefix)
            self.root_stream_started.set()
            await self.release_root.wait()
            yield ProviderStreamEvent(type="text_delta", text=" + root suffix")
            yield ProviderStreamEvent(type="done")
            return

        task = next(
            name
            for name in self.first_chunks
            if any(name in message.text_content for message in messages)
        )
        label = "first" if task.startswith("first") else "second"
        yield ProviderStreamEvent(type="text_delta", text=self.child_prefixes[task])
        self.first_chunks[task].set()
        await self.releases[task].wait()
        yield ProviderStreamEvent(type="text_delta", text=f" + {label} child suffix")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class TuiPlanConfirmationProvider:
    provider_id = "tui-plan-confirmation"

    def __init__(
        self,
        options: list[dict[str, str]] | None = None,
        *,
        plan: str = "# Release plan\n\n1. Inspect.\n2. Ship safely.\n3. Verify.",
    ) -> None:
        self.options = options or []
        self.plan = plan
        self.stream_count = 0
        self.plan_path: str | None = None

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, object]] | None = None,
        request_context=None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del model, system_prompt, tools
        if self.stream_count == 0:
            scaffold = next(
                (
                    message.text_content
                    for message in reversed(messages)
                    if "<plan-context>" in message.text_content
                ),
                None,
            )
            assert scaffold is not None, "Plan context was not delivered to the provider"
            match = re.search(r"Plan file: (.+)", scaffold)
            assert match is not None
            self.plan_path = match.group(1)
            call = ToolCallRequest(
                id="call_write_plan",
                name="Write",
                arguments={
                    "path": self.plan_path,
                    "content": self.plan,
                },
            )
        elif self.stream_count == 1:
            call = ToolCallRequest(
                id="call_exit_plan",
                name="ExitPlanMode",
                arguments={"options": self.options} if self.options else {},
            )
        else:
            self.stream_count += 1
            yield ProviderStreamEvent(type="text_delta", text="Decision received.")
            yield ProviderStreamEvent(type="done")
            return
        self.stream_count += 1
        yield ProviderStreamEvent.from_tool_call(call)
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )
