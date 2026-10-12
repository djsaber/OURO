from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Literal

from ouro.core.cancellation import AbortSignal
from ouro.local_processes import close_process_transports, start_process


class ProcessAborted(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


DEFAULT_PROCESS_OUTPUT_LIMIT = 100_000
_PROCESS_READ_SIZE = 64 * 1024
_PROCESS_PIPE_SETTLE_TIMEOUT_SECONDS = 0.5


@dataclass
class _BoundedProcessOutput:
    """Drain both process pipes while retaining one shared bounded prefix."""

    limit: int
    stdout: bytearray = dataclass_field(default_factory=bytearray)
    stderr: bytearray = dataclass_field(default_factory=bytearray)
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def retained_bytes(self) -> int:
        return len(self.stdout) + len(self.stderr)

    def append(self, stream_name: str, chunk: bytes) -> None:
        target = self.stdout if stream_name == "stdout" else self.stderr
        available = max(0, self.limit - self.retained_bytes)
        if available:
            target.extend(chunk[:available])
        if len(chunk) > available:
            if stream_name == "stdout":
                self.stdout_truncated = True
            else:
                self.stderr_truncated = True

    def render(self) -> tuple[str, str]:
        return (
            _bounded_text(
                bytes(self.stdout).decode(errors="replace"),
                self.limit,
                truncated=self.stdout_truncated,
            ),
            _bounded_text(
                bytes(self.stderr).decode(errors="replace"),
                self.limit,
                truncated=self.stderr_truncated,
            ),
        )


async def run_process(
    arguments: Sequence[str],
    *,
    cwd: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 30,
    max_output_chars: int = DEFAULT_PROCESS_OUTPUT_LIMIT,
    poll_interval: float = 0.05,
    signal: AbortSignal | None = None,
    shell: bool = False,
    on_output: Callable[[Literal["stdout", "stderr"], bytes], Awaitable[None]] | None = None,
) -> ProcessResult:
    """Execute a local command through bounded output and process-tree settlement."""
    if not arguments or (shell and len(arguments) != 1):
        raise ValueError("process arguments must be nonempty; shell execution takes one command")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("process timeout must be finite and positive")
    if signal is not None and signal.is_aborted():
        raise ProcessAborted(signal.reason)
    owner = await start_process(
        *arguments,
        shell=shell,
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        return await _collect_process(
            owner.process,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            poll_interval=poll_interval,
            signal=signal,
            on_output=on_output,
            terminate=owner.terminate,
        )
    finally:
        await owner.close()


async def _collect_process(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float,
    max_output_chars: int,
    poll_interval: float,
    signal: AbortSignal | None,
    terminate: Callable[[], Awaitable[None]],
    on_output: Callable[[Literal["stdout", "stderr"], bytes], Awaitable[None]] | None = None,
) -> ProcessResult:
    assert process.stdout is not None
    assert process.stderr is not None
    output = _BoundedProcessOutput(max_output_chars)
    stdout_task = asyncio.create_task(
        _drain_process_stream(process.stdout, output, "stdout", on_output)
    )
    stderr_task = asyncio.create_task(
        _drain_process_stream(process.stderr, output, "stderr", on_output)
    )
    wait_task = asyncio.create_task(process.wait())
    tasks = (wait_task, stdout_task, stderr_task)
    timed_out = False
    aborted = False
    parent_exited = False
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        while not all(task.done() for task in tasks):
            if process.returncode is not None and not parent_exited:
                parent_exited = True
                await terminate()
            if signal is not None and signal.is_aborted():
                await terminate()
                aborted = True
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await terminate()
                timed_out = True
                break
            await asyncio.wait(tasks, timeout=min(poll_interval, remaining))
            for task in tasks:
                if task.done():
                    task.result()

        if timed_out or aborted:
            await _settle_process_tasks(process, tasks)
        else:
            await asyncio.gather(*tasks)
        if aborted:
            raise ProcessAborted(signal.reason if signal is not None else "Process aborted")
        stdout, stderr = output.render()
        return ProcessResult(
            stdout=stdout,
            stderr=stderr,
            returncode=process.returncode,
            timed_out=timed_out,
            stdout_truncated=output.stdout_truncated,
            stderr_truncated=output.stderr_truncated,
        )
    finally:
        if not all(task.done() for task in tasks):
            with suppress(Exception):
                await terminate()
            await _settle_process_tasks(process, tasks)


def _bounded_text(text: str, limit: int, *, truncated: bool) -> str:
    if not truncated and len(text) <= limit:
        return text
    marker = f"\n...[output truncated at {limit} characters]"
    prefix_length = max(0, limit - len(marker))
    return f"{text[:prefix_length]}{marker}"


async def _drain_process_stream(
    stream: asyncio.StreamReader,
    output: _BoundedProcessOutput,
    stream_name: Literal["stdout", "stderr"],
    on_output: Callable[[Literal["stdout", "stderr"], bytes], Awaitable[None]] | None = None,
) -> None:
    while chunk := await stream.read(_PROCESS_READ_SIZE):
        output.append(stream_name, chunk)
        if on_output is not None:
            await on_output(stream_name, chunk)
    if on_output is not None:
        await on_output(stream_name, b"")


async def _settle_process_tasks(
    process: asyncio.subprocess.Process,
    tasks: tuple[asyncio.Task[int], asyncio.Task[None], asyncio.Task[None]],
) -> None:
    _, pending = await asyncio.wait(
        tasks,
        timeout=_PROCESS_PIPE_SETTLE_TIMEOUT_SECONDS,
    )
    if not pending:
        return
    with suppress(ProcessLookupError):
        process.kill()
    close_process_transports(process)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
