from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from inspect import signature
from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator

from ouro.core.base import OuroModel
from ouro.core.callbacks import invoke_owned_callback
from ouro.core.cancellation import AbortSignal
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.interactions import ToolInputResolution
from ouro.core.tools import (
    PermissionMode,
    ToolBatchBehavior,
    ToolDefinition,
    ToolEffect,
    ToolExecutionOutcome,
    ToolInputRequest,
    ToolInvocation,
    ToolOutcomeStatus,
    ToolPermissionControl,
    ToolResult,
    ToolSource,
)

__all__ = [
    "PermissionMode",
    "Tool",
    "ToolDefinition",
    "ToolBatchBehavior",
    "ToolEffect",
    "ToolExecutionAborted",
    "ToolExecutionContext",
    "ToolPolicyError",
    "ToolExecutionOutcome",
    "ToolInputRequest",
    "ToolInvocation",
    "ToolOutcomeStatus",
    "ToolPermissionControl",
    "ToolProcessResult",
    "ToolResult",
    "ToolSource",
]


class ToolExecutionAborted(Exception):
    def __init__(self, reason: str = "Tool execution aborted") -> None:
        super().__init__(reason)
        self.reason = reason


class ToolPolicyError(ValueError):
    """Raised when a tool request violates an execution policy."""


@dataclass(frozen=True)
class ToolProcessResult:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


_DEFAULT_PROCESS_OUTPUT_LIMIT = 100_000
_PROCESS_READ_SIZE = 64 * 1024
_PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 5.0
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


@dataclass
class ToolExecutionContext:
    harness: HarnessCallSnapshot | None = None
    signal: AbortSignal | None = None
    poll_interval: float = 0.05
    workspace: str | Path | None = None
    artifact_root: str | Path | None = None
    readable_roots: tuple[Path, ...] = ()
    workspace_only: bool = False
    max_output_chars: int | None = _DEFAULT_PROCESS_OUTPUT_LIMIT
    max_output_media_chars: int = 20 * 1024 * 1024

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

    def resolve_path(self, path: str, *, read_only: bool = False) -> Path:
        """Resolve a tool path and enforce the Host-configured path boundaries."""

        candidate = Path(path).expanduser()
        workspace = self._resolved_workspace()
        if not candidate.is_absolute():
            candidate = (workspace or Path.cwd()) / candidate
        candidate = candidate.resolve(strict=False)
        if self.workspace_only:
            artifact_root = self._resolved_artifact_root()
            allowed_roots = tuple(root for root in (workspace, artifact_root) if root is not None)
            if read_only:
                allowed_roots += tuple(root.resolve() for root in self.readable_roots)
            if not allowed_roots:
                raise ToolPolicyError(
                    "workspace-only tool execution requires a configured workspace or artifact root"
                )
            if not any(candidate.is_relative_to(root) for root in allowed_roots):
                raise ToolPolicyError(
                    f"path is outside the configured workspace and artifact roots: {candidate}"
                )
        return candidate

    def resolve_working_directory(self, cwd: str | None) -> Path | None:
        resolved: Path | None
        if cwd is not None:
            resolved = self.resolve_path(cwd)
        elif self.workspace is not None:
            resolved = self._resolved_workspace()
        else:
            resolved = None
        if resolved is not None and not resolved.is_dir():
            raise ToolPolicyError(f"working directory does not exist: {resolved}")
        return resolved

    def limit_output(self, text: str) -> tuple[str, bool]:
        limit = self.max_output_chars
        if limit is None or len(text) <= limit:
            return text, False
        return _bounded_text(text, limit, truncated=True), True

    def _resolved_workspace(self) -> Path | None:
        if self.workspace is None:
            return None
        return Path(self.workspace).expanduser().resolve(strict=False)

    def _resolved_artifact_root(self) -> Path | None:
        if self.artifact_root is None:
            return None
        return Path(self.artifact_root).expanduser().resolve(strict=False)

    async def run_shell(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> ToolProcessResult:
        self.raise_if_aborted()
        if os.name == "nt":
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        return await self._collect_process(process, timeout_seconds=timeout_seconds)

    async def run_process(
        self,
        arguments: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
    ) -> ToolProcessResult:
        """Run one resolved executable without inserting an intermediary shell.

        Builtin command tools already know the selected shell executable and its
        argument contract. Launching that executable through another platform shell
        changes quoting and process ownership on Windows, so the Host process boundary
        accepts argv directly and supervises the process it actually owns.
        """

        self.raise_if_aborted()
        if not arguments:
            raise ValueError("process arguments cannot be empty")
        process_environment = dict(environment) if environment is not None else None
        if os.name == "nt":
            process = await asyncio.create_subprocess_exec(
                *arguments,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=process_environment,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=process_environment,
                start_new_session=True,
            )
        return await self._collect_process(process, timeout_seconds=timeout_seconds)

    async def _collect_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        timeout_seconds: float,
    ) -> ToolProcessResult:
        assert process.stdout is not None
        assert process.stderr is not None
        output = _BoundedProcessOutput(self.max_output_chars or _DEFAULT_PROCESS_OUTPUT_LIMIT)
        stdout_task = asyncio.create_task(_drain_process_stream(process.stdout, output, "stdout"))
        stderr_task = asyncio.create_task(_drain_process_stream(process.stderr, output, "stderr"))
        wait_task = asyncio.create_task(process.wait())
        tasks = (wait_task, stdout_task, stderr_task)
        timed_out = False
        aborted = False
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            while not all(task.done() for task in tasks):
                if self.is_aborted:
                    await _kill_process_tree(process)
                    aborted = True
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await _kill_process_tree(process)
                    timed_out = True
                    break
                await asyncio.wait(tasks, timeout=min(self.poll_interval, remaining))

            if timed_out or aborted:
                await _settle_process_tasks(process, tasks)
            else:
                await asyncio.gather(*tasks)
            if aborted:
                raise ToolExecutionAborted(self.abort_reason)
            stdout, stderr = output.render()
            return ToolProcessResult(
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
                    await _kill_process_tree(process)
                await _settle_process_tasks(process, tasks)


def _bounded_text(text: str, limit: int, *, truncated: bool) -> str:
    if not truncated and len(text) <= limit:
        return text
    marker = f"\n...[tool output truncated at {limit} characters]"
    prefix_length = max(0, limit - len(marker))
    return f"{text[:prefix_length]}{marker}"


async def _drain_process_stream(
    stream: asyncio.StreamReader,
    output: _BoundedProcessOutput,
    stream_name: str,
) -> None:
    while chunk := await stream.read(_PROCESS_READ_SIZE):
        output.append(stream_name, chunk)


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
    _close_process_transports(process)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def _close_process_transports(process: asyncio.subprocess.Process) -> None:
    """Close parent-owned asyncio pipe handles after forced process termination.

    ``StreamReader`` has no public close API. On Windows, cancelling a pending
    Proactor read without closing its transport leaks the OS pipe handle, so the
    process boundary closes the transports it created before cancelling readers.
    """

    process_transport = getattr(process, "_transport", None)
    get_pipe_transport = getattr(process_transport, "get_pipe_transport", None)
    if callable(get_pipe_transport):
        for file_descriptor in (1, 2):
            pipe_transport = get_pipe_transport(file_descriptor)
            close_pipe = getattr(pipe_transport, "close", None)
            if callable(close_pipe):
                close_pipe()
    close_process = getattr(process_transport, "close", None)
    if callable(close_process):
        close_process()


async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Stop the shell and descendants so inherited output pipes can reach EOF."""

    if os.name == "nt":
        killer: asyncio.subprocess.Process | None = None
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(
                killer.wait(),
                timeout=_PROCESS_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except OSError, TimeoutError:
            if killer is not None:
                with suppress(ProcessLookupError):
                    killer.kill()
    else:
        kill_process_group = getattr(os, "killpg", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if kill_process_group is not None and sigkill is not None:
            with suppress(ProcessLookupError):
                kill_process_group(process.pid, sigkill)
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()


ToolExecutionResult = ToolResult | ToolInputRequest
ToolHandler = Callable[
    [ToolInvocation, ToolExecutionContext], ToolExecutionResult | Awaitable[ToolExecutionResult]
]
ToolResumeHandler = Callable[
    [ToolInvocation, ToolInputResolution, ToolInputRequest, ToolExecutionContext],
    ToolExecutionResult | Awaitable[ToolExecutionResult],
]


class Tool(OuroModel):
    definition: ToolDefinition
    handler: ToolHandler = Field(exclude=True)
    resume_handler: ToolResumeHandler | None = Field(default=None, exclude=True)

    @field_validator("handler", "resume_handler")
    @classmethod
    def validate_callback_signature(
        cls, handler: ToolHandler | ToolResumeHandler | None, info: ValidationInfo
    ) -> ToolHandler | ToolResumeHandler | None:
        if handler is not None:
            arguments = (None, None) if info.field_name == "handler" else (None, None, None, None)
            try:
                signature(handler).bind(*arguments)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid {info.field_name} signature: {exc}") from exc
        return handler

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        context: ToolExecutionContext | None = None,
        signal: AbortSignal | None = None,
    ) -> ToolExecutionResult:
        context = context or ToolExecutionContext(signal=signal)
        return await invoke_owned_callback(self.handler, invocation, context)

    async def resume(
        self,
        invocation: ToolInvocation,
        resolution: ToolInputResolution,
        *,
        request: ToolInputRequest,
        context: ToolExecutionContext | None = None,
        signal: AbortSignal | None = None,
    ) -> ToolExecutionResult:
        if self.resume_handler is None:
            raise RuntimeError(f"tool does not support resume: {self.definition.name}")
        context = context or ToolExecutionContext(signal=signal)
        return await invoke_owned_callback(
            self.resume_handler, invocation, resolution, request, context
        )
