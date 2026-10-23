from __future__ import annotations

import asyncio
import codecs
import math
from collections.abc import Awaitable, Callable
from typing import Literal

from ouro.plugins.catalog.gates import GenerationGate
from ouro.plugins.invocation import current_invocation
from ouro.sdk.plugins.processes import ProcessOutput, ProcessRequest, ProcessResult
from ouro.tools.base import ToolExecutionContext


class PluginProcessService:
    """Run an argv under the calling generation's permission and cancellation gate."""

    def __init__(self, gate: GenerationGate, permissions: frozenset[str]) -> None:
        self.gate = gate
        self.permissions = permissions

    async def run(
        self,
        request: ProcessRequest,
        *,
        on_output: Callable[[ProcessOutput], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        if "process.spawn" not in self.permissions:
            raise PermissionError("plugin process execution requires process.spawn")
        invocation = current_invocation.get()
        if invocation is not None:
            invocation.require_active()
            if not invocation.writable:
                raise PermissionError("a read-only callback cannot start a process")
        if not request.arguments or any(not isinstance(item, str) for item in request.arguments):
            raise ValueError("process arguments must be a nonempty sequence of strings")
        if not math.isfinite(request.timeout_seconds) or request.timeout_seconds <= 0:
            raise ValueError("process timeout must be finite and positive")
        sequence = 0
        lock = asyncio.Lock()
        decoders = {
            name: codecs.getincrementaldecoder("utf-8")("replace") for name in ("stdout", "stderr")
        }

        async def output(stream: Literal["stdout", "stderr"], chunk: bytes) -> None:
            nonlocal sequence
            text = decoders[stream].decode(chunk, final=not chunk)
            if on_output is not None and text:
                async with lock:
                    sequence += 1
                    await on_output(ProcessOutput(stream, text, sequence))

        async with self.gate.invocation():
            context = ToolExecutionContext(
                workspace=invocation.workspace if invocation is not None else None,
                workspace_only=invocation.workspace_only if invocation is not None else False,
            )
            result = await context.run_process(
                request.arguments,
                cwd=context.resolve_working_directory(request.cwd),
                environment=request.environment,
                timeout_seconds=request.timeout_seconds,
                on_output=output if on_output is not None else None,
            )
        return ProcessResult(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            timed_out=result.timed_out,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
        )
