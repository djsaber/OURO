from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

PermissionMode = Literal["auto", "approval", "deny"]
ToolPermissionControl = Literal["host", "tool"]
ToolEffect = Literal["read_only", "state_changing", "unknown"]
ToolBatchBehavior = Literal["compatible", "exclusive"]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
SessionInputMode = Literal["follow_up", "steer"]
ContextFragmentPersistence = Literal["ephemeral", "message"]
RunStatus = Literal[
    "queued",
    "running",
    "waiting_user",
    "waiting_approval",
    "completed",
    "aborted",
    "failed",
    "paused",
    "recoverable",
]
RunOrigin = Literal["user", "follow_up", "replay", "host", "plugin"]
HookEvent = Literal[
    "after_run",
    "after_context_build",
    "before_tool_call",
    "after_tool_call",
    "on_event",
]
StopReason = Literal["disable", "restart", "upgrade", "shutdown", "startup_failed"]


@dataclass(frozen=True)
class PluginIdentity:
    plugin_id: str
    version: str
    plugin_class: Literal["builtin", "user"]
    release_id: str
    package_digest: str | None
    builtin_contract_hash: str | None
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
    artifact_root: str | None = None


class ToolExecutionContext(Protocol):
    """Host-owned, request-scoped services available to an in-process tool.

    Worker tools receive the same workspace fields in ``ToolCall`` and are
    cancelled through the worker protocol's ``cancel`` notification.
    """

    @property
    def workspace(self) -> str | None: ...

    @property
    def workspace_only(self) -> bool: ...

    @property
    def is_aborted(self) -> bool: ...

    @property
    def abort_reason(self) -> str: ...

    def raise_if_aborted(self) -> None: ...

    async def sleep(self, seconds: float) -> None: ...

    def resolve_path(self, path: str) -> str: ...

    def resolve_working_directory(self, cwd: str | None = None) -> str | None: ...


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    session_id: str
    run_id: str
    tool_name: str
    arguments: Mapping[str, object]
    workspace: str | None = None
    workspace_only: bool = False


@dataclass(frozen=True)
class ToolOutput:
    content: str = ""
    structured: Mapping[str, object] = field(default_factory=dict)
    resource_refs: tuple[str, ...] = ()
    is_error: bool = False


@dataclass(frozen=True)
class ToolInputRequest:
    """Suspend the current Run and let the host collect an opaque JSON answer."""

    data: JsonValue


@dataclass(frozen=True)
class AnswerResolution:
    value: JsonValue
    kind: Literal["answer"] = "answer"
    resolved_by: Literal["user", "host_policy"] = "user"
    resolver_id: str | None = None


@dataclass(frozen=True)
class UnattendedResolution:
    resolver_id: str
    reason: str
    kind: Literal["unattended"] = "unattended"
    resolved_by: Literal["host_policy"] = "host_policy"


ToolInputResolution = AnswerResolution | UnattendedResolution


ToolHandlerResult = (
    ToolOutput | ToolInputRequest | str | Awaitable[ToolOutput | ToolInputRequest | str]
)
ToolHandler = (
    Callable[[ToolCall], ToolHandlerResult]
    | Callable[[ToolCall, ToolExecutionContext], ToolHandlerResult]
)


@dataclass(frozen=True)
class ToolResumeCall:
    tool_call_id: str
    session_id: str
    run_id: str
    tool_name: str
    arguments: Mapping[str, object]
    request: JsonValue
    resolution: ToolInputResolution
    workspace: str | None = None
    workspace_only: bool = False

    @property
    def answer(self) -> JsonValue:
        """Return the opaque answer for an answered interaction."""

        if not isinstance(self.resolution, AnswerResolution):
            raise ValueError("unattended tool input resolution has no answer")
        return self.resolution.value


ToolResumeHandler = (
    Callable[[ToolResumeCall], ToolHandlerResult]
    | Callable[[ToolResumeCall, ToolExecutionContext], ToolHandlerResult]
)


@dataclass(frozen=True)
class SessionInput:
    """Request a synthetic, provenance-bearing input through the Host runtime."""

    prompt: str
    mode: SessionInputMode = "follow_up"
    title: str | None = None


@dataclass(frozen=True)
class ToolContribution:
    id: str
    exposed_name: str
    description: str
    handler: ToolHandler
    parameters_schema: Mapping[str, object] = field(default_factory=dict)
    result_schema: Mapping[str, object] = field(default_factory=dict)
    permission_mode: PermissionMode = "approval"
    permission_control: ToolPermissionControl = "host"
    effect: ToolEffect = "unknown"
    batch_behavior: ToolBatchBehavior = "compatible"
    resume_handler: ToolResumeHandler | None = None
    supports_unattended: bool = False


ToolPolicyAction = Literal["ignore", "require_approval", "deny"]


@dataclass(frozen=True)
class ToolPolicyCall:
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    canonical_tool_id: str
    owner_id: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    source: str = "builtin"
    effect: ToolEffect = "unknown"
    batch_behavior: ToolBatchBehavior = "compatible"
    permission_control: ToolPermissionControl = "host"
    workspace: str | None = None


@dataclass(frozen=True)
class ToolPolicyDecision:
    action: ToolPolicyAction = "ignore"
    reason: str | None = None


ToolPolicyHandler = Callable[
    [ToolPolicyCall],
    ToolPolicyDecision | Awaitable[ToolPolicyDecision],
]


@dataclass(frozen=True)
class ToolPolicyContribution:
    id: str
    handler: ToolPolicyHandler
    priority: int = 100
    timeout_seconds: float = 5.0


TurnPolicyAction = Literal["allow", "continue"]


@dataclass(frozen=True)
class TurnCompletionCall:
    session_id: str
    run_id: str
    turn_id: str
    assistant_text: str
    assistant_reasoning: str = ""


@dataclass(frozen=True)
class TurnPolicyDecision:
    action: TurnPolicyAction = "allow"
    feedback: str | None = None


TurnPolicyHandler = Callable[
    [TurnCompletionCall],
    TurnPolicyDecision | Awaitable[TurnPolicyDecision],
]


@dataclass(frozen=True)
class TurnPolicyContribution:
    id: str
    handler: TurnPolicyHandler
    priority: int = 100
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class RunOutcome:
    session_id: str
    run_id: str
    status: RunStatus
    origin: RunOrigin
    final_text: str = ""
    error: str | None = None
    usage: Mapping[str, int | float] = field(default_factory=dict)


@dataclass(frozen=True)
class HookCall:
    event: HookEvent
    session_id: str | None = None
    run_id: str | None = None
    data: Mapping[str, object] = field(default_factory=dict)
    run_outcome: RunOutcome | None = None


@dataclass(frozen=True)
class HookOutput:
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
    session_inputs: tuple[SessionInput, ...] = ()


CommandHandler = Callable[
    [CommandCall],
    CommandOutput
    | str
    | Mapping[str, object]
    | Awaitable[CommandOutput | str | Mapping[str, object]],
]

ContinuationAction = Literal["ignore", "continue", "complete", "pause", "fail"]


@dataclass(frozen=True)
class ContinuationDecision:
    action: ContinuationAction = "ignore"
    prompt: str | None = None
    mode: SessionInputMode = "follow_up"
    title: str | None = None
    reason: str | None = None
    state_revision: int | None = None


ContinuationHandler = Callable[
    [RunOutcome],
    ContinuationDecision | Awaitable[ContinuationDecision],
]


@dataclass(frozen=True)
class ContinuationPolicyContribution:
    id: str
    handler: ContinuationHandler
    priority: int = 100
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class CommandContribution:
    id: str
    description: str
    handler: CommandHandler
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class ContextCall:
    session_id: str
    run_id: str
    message_count: int
    model_round: int


@dataclass(frozen=True)
class ContextOutput:
    fragments: tuple[str | ContextFragment, ...] = ()


@dataclass(frozen=True)
class ContextFragment:
    """Model context supplied for one request or persisted as a canonical message."""

    text: str
    persistence: ContextFragmentPersistence = "ephemeral"


ContextHandler = Callable[
    [ContextCall],
    ContextOutput
    | str
    | tuple[str | ContextFragment, ...]
    | Awaitable[ContextOutput | str | tuple[str | ContextFragment, ...]],
]


@dataclass(frozen=True)
class ContextContribution:
    id: str
    handler: ContextHandler
    priority: int = 100
    timeout_seconds: float = 10.0
    failure_mode: Literal["omit", "block"] = "omit"


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
    continuation_policies: list[ContinuationPolicyContribution] = field(default_factory=list)
    turn_policies: list[TurnPolicyContribution] = field(default_factory=list)
    tool_policies: list[ToolPolicyContribution] = field(default_factory=list)


class PluginInstance(Protocol):
    async def prepare(self) -> ContributionSet: ...

    async def start(self) -> None: ...

    async def stop(self, reason: StopReason, deadline: float) -> None: ...


PluginFactory = Callable[[PluginContext], PluginInstance | Awaitable[PluginInstance]]


__all__ = [
    "AnswerResolution",
    "BackgroundServiceContribution",
    "CommandCall",
    "CommandContribution",
    "CommandOutput",
    "CompactionCall",
    "CompactionContribution",
    "CompactionOutput",
    "ContinuationAction",
    "ContinuationDecision",
    "ContinuationPolicyContribution",
    "ContextContribution",
    "ContextCall",
    "ContextFragment",
    "ContextFragmentPersistence",
    "ContextOutput",
    "ContributionSet",
    "DiagnosticEmitter",
    "HookCall",
    "HookContribution",
    "HookOutput",
    "JsonValue",
    "PluginContext",
    "PluginCredentialResolver",
    "PluginDataStore",
    "PluginFactory",
    "PluginIdentity",
    "PluginInstance",
    "PluginLogger",
    "PluginProvenanceSummary",
    "PluginTaskGroup",
    "RunOutcome",
    "RunOrigin",
    "RunStatus",
    "SessionInput",
    "SessionInputMode",
    "StopReason",
    "ToolCall",
    "ToolBatchBehavior",
    "ToolContribution",
    "ToolEffect",
    "ToolExecutionContext",
    "ToolInputRequest",
    "ToolInputResolution",
    "ToolOutput",
    "ToolPolicyAction",
    "ToolPolicyCall",
    "ToolPolicyContribution",
    "ToolPolicyDecision",
    "ToolPermissionControl",
    "ToolResumeCall",
    "UnattendedResolution",
    "TurnCompletionCall",
    "TurnPolicyAction",
    "TurnPolicyContribution",
    "TurnPolicyDecision",
]
