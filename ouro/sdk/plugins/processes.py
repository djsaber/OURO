from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ProcessRequest:
    arguments: tuple[str, ...]
    cwd: str | None = None
    timeout_seconds: float = 30
    environment: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ProcessOutput:
    stream: Literal["stdout", "stderr"]
    text: str
    sequence: int


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class PluginProcesses(Protocol):
    async def run(
        self,
        request: ProcessRequest,
        *,
        on_output: Callable[[ProcessOutput], Awaitable[None]] | None = None,
    ) -> ProcessResult: ...
