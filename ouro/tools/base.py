from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import signature
from pathlib import Path
from typing import Literal

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
from ouro.local_processes.commands import (
    DEFAULT_PROCESS_OUTPUT_LIMIT,
    ProcessAborted,
    ProcessResult,
    run_process,
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
    "ProcessResult",
    "ToolResult",
    "ToolSource",
]


class ToolExecutionAborted(Exception):
    def __init__(self, reason: str = "Tool execution aborted") -> None:
        super().__init__(reason)
        self.reason = reason


class ToolPolicyError(ValueError):
    """Raised when a tool request violates an execution policy."""


@dataclass
class ToolExecutionContext:
    harness: HarnessCallSnapshot | None = None
    signal: AbortSignal | None = None
    poll_interval: float = 0.05
    workspace: str | Path | None = None
    artifact_root: str | Path | None = None
    readable_roots: tuple[Path, ...] = ()
    workspace_only: bool = False
    max_output_chars: int | None = DEFAULT_PROCESS_OUTPUT_LIMIT
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
    ) -> ProcessResult:
        return await self._run_process(
            (command,), shell=True, cwd=cwd, timeout_seconds=timeout_seconds
        )

    async def run_process(
        self,
        arguments: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
        on_output: Callable[[Literal["stdout", "stderr"], bytes], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        return await self._run_process(
            arguments,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            environment=environment,
            on_output=on_output,
        )

    async def _run_process(
        self,
        arguments: Sequence[str],
        *,
        cwd: str | Path | None,
        timeout_seconds: float,
        shell: bool = False,
        environment: Mapping[str, str] | None = None,
        on_output: Callable[[Literal["stdout", "stderr"], bytes], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        self.raise_if_aborted()
        try:
            return await run_process(
                arguments,
                shell=shell,
                cwd=cwd,
                environment=environment,
                timeout_seconds=timeout_seconds,
                signal=self.signal,
                max_output_chars=self.max_output_chars or DEFAULT_PROCESS_OUTPUT_LIMIT,
                poll_interval=self.poll_interval,
                on_output=on_output,
            )
        except ProcessAborted as exc:
            raise ToolExecutionAborted(exc.reason) from exc


def _bounded_text(text: str, limit: int, *, truncated: bool) -> str:
    if not truncated and len(text) <= limit:
        return text
    marker = f"\n...[tool output truncated at {limit} characters]"
    prefix_length = max(0, limit - len(marker))
    return f"{text[:prefix_length]}{marker}"


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
