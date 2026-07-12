from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from inspect import Parameter, signature

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.cancellation import AbortSignal
from ouro.core.tools import (
    PermissionMode,
    ToolDefinition,
    ToolExecutionOutcome,
    ToolInvocation,
    ToolOutcomeStatus,
    ToolResult,
    ToolSource,
)

__all__ = [
    "PermissionMode",
    "Tool",
    "ToolDefinition",
    "ToolExecutionAborted",
    "ToolExecutionContext",
    "ToolExecutionOutcome",
    "ToolInvocation",
    "ToolOutcomeStatus",
    "ToolProcessResult",
    "ToolResult",
    "ToolSource",
]


class ToolExecutionAborted(Exception):
    def __init__(self, reason: str = "Tool execution aborted") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ToolProcessResult:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    aborted: bool = False


@dataclass
class ToolExecutionContext:
    signal: AbortSignal | None = None
    poll_interval: float = 0.05

    @property
    def is_aborted(self) -> bool:
        return self.signal.is_aborted() if self.signal is not None else False

    @property
    def abort_reason(self) -> str:
        return self.signal.reason if self.signal is not None else "Tool execution aborted"

    def raise_if_aborted(self) -> None:
        if self.is_aborted:
            raise ToolExecutionAborted(self.abort_reason)

    async def sleep(self, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            self.raise_if_aborted()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.poll_interval, remaining))

    async def run_shell(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> ToolProcessResult:
        self.raise_if_aborted()
        if os.name == "nt":
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        communicate_task = asyncio.create_task(process.communicate())
        timed_out = False
        aborted = False
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not communicate_task.done():
            if self.is_aborted:
                await _kill_process_tree(process)
                aborted = True
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _kill_process_tree(process)
                timed_out = True
                break
            await asyncio.wait({communicate_task}, timeout=min(self.poll_interval, remaining))

        if timed_out or aborted:
            stdout, stderr = await communicate_task
        else:
            stdout, stderr = communicate_task.result()
        return ToolProcessResult(
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            returncode=process.returncode,
            timed_out=timed_out,
            aborted=aborted,
        )


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Stop the shell and descendants so inherited output pipes can reach EOF."""

    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        kill_process_group = getattr(os, "killpg", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if kill_process_group is not None and sigkill is not None:
            with suppress(ProcessLookupError):
                kill_process_group(process.pid, sigkill)
    with suppress(ProcessLookupError):
        process.kill()


ToolHandler = Callable[..., ToolResult | Awaitable[ToolResult]]


class Tool(OuroModel):
    definition: ToolDefinition
    handler: ToolHandler = Field(exclude=True)

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        context: ToolExecutionContext | None = None,
        signal: AbortSignal | None = None,
    ) -> ToolResult:
        context = context or ToolExecutionContext(signal=signal)
        raw_result = _call_handler(self.handler, invocation, context)
        if isinstance(raw_result, Awaitable):
            return await raw_result
        return raw_result


def _call_handler(
    handler: ToolHandler,
    invocation: ToolInvocation,
    context: ToolExecutionContext,
) -> ToolResult | Awaitable[ToolResult]:
    try:
        parameters = tuple(signature(handler).parameters.values())
    except (TypeError, ValueError):
        return handler(invocation)

    if any(parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters):
        return handler(invocation, context=context, signal=context.signal)
    if any(parameter.name == "context" for parameter in parameters):
        return handler(invocation, context=context)
    if any(parameter.name == "signal" for parameter in parameters):
        return handler(invocation, signal=context.signal)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2:
        return handler(invocation, context)
    return handler(invocation)
