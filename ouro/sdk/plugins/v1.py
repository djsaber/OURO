from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

PermissionMode = Literal["auto", "approval", "deny"]
HookEvent = Literal[
    "before_run",
    "after_run",
    "before_context_build",
    "after_context_build",
    "before_model_call",
    "after_model_response",
    "before_tool_call",
    "after_tool_call",
    "on_approval_requested",
    "on_event",
    "on_error",
]
StopReason = Literal["disable", "restart", "upgrade", "shutdown", "startup_failed"]


@dataclass(frozen=True)
class PluginIdentity:
    plugin_id: str
    version: str
    package_digest: str
    generation_id: str


@dataclass(frozen=True)
class PluginProvenanceSummary:
    source_kind: str
    public_source: str


class PluginDataStore(Protocol):
    def read_bytes(self, relative_path: str) -> bytes: ...

    def write_bytes(self, relative_path: str, value: bytes) -> None: ...

    def delete(self, relative_path: str) -> None: ...


class PluginCredentialResolver(Protocol):
    def resolve(self, slot: str) -> str: ...


class PluginTaskGroup(Protocol):
    def start(self, coroutine: Awaitable[object], *, name: str | None = None) -> None: ...


class PluginLogger(Protocol):
    def debug(self, message: str, *args: object) -> None: ...

    def info(self, message: str, *args: object) -> None: ...

    def warning(self, message: str, *args: object) -> None: ...

    def error(self, message: str, *args: object) -> None: ...


class DiagnosticEmitter(Protocol):
    def emit(
        self,
        code: str,
        message: str,
        *,
        severity: Literal["info", "warning", "error"] = "warning",
    ) -> None: ...


@dataclass(frozen=True)
class PluginContext:
    identity: PluginIdentity
    provenance: PluginProvenanceSummary
    configuration: Mapping[str, object]
    data: PluginDataStore
    credentials: PluginCredentialResolver
    tasks: PluginTaskGroup
    logger: PluginLogger
    diagnostics: DiagnosticEmitter
    permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    session_id: str
    run_id: str
    tool_name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class ToolOutput:
    content: str = ""
    structured: Mapping[str, object] = field(default_factory=dict)
    resource_refs: tuple[str, ...] = ()
    is_error: bool = False


ToolHandler = Callable[[ToolCall], ToolOutput | str | Awaitable[ToolOutput | str]]


@dataclass(frozen=True)
class ToolContribution:
    id: str
    exposed_name: str
    description: str
    handler: ToolHandler
    parameters_schema: Mapping[str, object] = field(default_factory=dict)
    result_schema: Mapping[str, object] = field(default_factory=dict)
    permission_mode: PermissionMode = "approval"


@dataclass(frozen=True)
class HookCall:
    event: HookEvent
    session_id: str | None = None
    run_id: str | None = None
    data: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HookOutput:
    added_context: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


HookHandler = Callable[[HookCall], HookOutput | None | Awaitable[HookOutput | None]]


@dataclass(frozen=True)
class HookContribution:
    id: str
    event: HookEvent
    handler: HookHandler
    priority: int = 100
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class CommandCall:
    command_id: str
    argument: str = ""
    session_id: str | None = None
    workspace: str | None = None


@dataclass(frozen=True)
class CommandOutput:
    output: str = ""
    data: Mapping[str, object] = field(default_factory=dict)


CommandHandler = Callable[
    [CommandCall],
    CommandOutput
    | str
    | Mapping[str, object]
    | Awaitable[CommandOutput | str | Mapping[str, object]],
]


@dataclass(frozen=True)
class CommandContribution:
    id: str
    description: str
    handler: CommandHandler
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class ContextContribution:
    id: str
    handler: HookHandler
    priority: int = 100


@dataclass(frozen=True)
class CompactionCall:
    session_id: str
    run_id: str | None
    previous_summary: str | None
    messages: tuple[Mapping[str, object], ...]
    input_token_budget: int
    summary_output_tokens: int


@dataclass(frozen=True)
class CompactionOutput:
    summary: str
    usage: Mapping[str, int | float] = field(default_factory=dict)


CompactionHandler = Callable[[CompactionCall], CompactionOutput | Awaitable[CompactionOutput]]

BackgroundHandler = Callable[[], None | Awaitable[None]]


@dataclass(frozen=True)
class BackgroundServiceContribution:
    id: str
    start: BackgroundHandler
    stop: BackgroundHandler


@dataclass(frozen=True)
class CompactionContribution:
    id: str
    version: str
    handler: CompactionHandler


@dataclass
class ContributionSet:
    tools: list[ToolContribution] = field(default_factory=list)
    hooks: list[HookContribution] = field(default_factory=list)
    commands: list[CommandContribution] = field(default_factory=list)
    context_providers: list[ContextContribution] = field(default_factory=list)
    compaction_strategies: list[CompactionContribution] = field(default_factory=list)
    background_services: list[BackgroundServiceContribution] = field(default_factory=list)


class PluginInstance(Protocol):
    async def prepare(self) -> ContributionSet: ...

    async def start(self) -> None: ...

    async def stop(self, reason: StopReason, deadline: float) -> None: ...


PluginFactory = Callable[[PluginContext], PluginInstance | Awaitable[PluginInstance]]


__all__ = [
    "BackgroundServiceContribution",
    "CommandCall",
    "CommandContribution",
    "CommandOutput",
    "CompactionCall",
    "CompactionContribution",
    "CompactionOutput",
    "ContextContribution",
    "ContributionSet",
    "DiagnosticEmitter",
    "HookCall",
    "HookContribution",
    "HookOutput",
    "PluginContext",
    "PluginCredentialResolver",
    "PluginDataStore",
    "PluginFactory",
    "PluginIdentity",
    "PluginInstance",
    "PluginLogger",
    "PluginProvenanceSummary",
    "PluginTaskGroup",
    "StopReason",
    "ToolCall",
    "ToolContribution",
    "ToolOutput",
]
