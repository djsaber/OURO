from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from .content import (
    ToolAudioContent,
    ToolContent,
    ToolImageContent,
    ToolTextContent,
    ToolVideoContent,
)
from .environment import PluginEnvironment
from .models import PluginModels
from .processes import PluginProcesses
from .providers import ProviderContribution
from .run_drivers import (
    AgentTask,
    DriverFinished,
    DriverResume,
    DriverSuspended,
    ModelResponse,
    ModelToolCall,
    NativeToolResult,
    RunAgents,
    RunDriverContext,
    RunDriverContribution,
    RunInput,
    RunInputs,
    RunModels,
    RunToolCall,
    RunTools,
    ToolBatchResult,
)
from .scheduling import PluginScheduler
from .services import PluginServices, Registration, ServiceContribution
from .state import (
    JsonValue,
    PluginRunControl,
    PluginSessionControl,
    PluginSessionState,
    PluginSessionStateView,
)

PermissionMode = Literal["auto", "approval", "deny"]
ToolPermissionControl = Literal["host", "tool"]
ToolEffect = Literal["read_only", "state_changing", "unknown"]
ToolBatchBehavior = Literal["compatible", "exclusive"]
SessionInputMode = Literal["follow_up", "steer"]
SessionInputRole = Literal["system", "user", "assistant", "tool_result"]
PromptInputSource = Literal["user", "host", "plugin", "delegation"]
SessionKind = Literal["primary", "subagent"]
ContextFragmentPersistence = Literal["ephemeral", "message"]
ContextFragmentTarget = Literal["message", "system_prompt"]
RunStatus = Literal[
    "queued",
    "running",
    "waiting_user",
    "waiting_approval",
    "waiting_child",
    "completed",
    "aborted",
    "failed",
    "paused",
    "recoverable",
]
RunOrigin = Literal["user", "follow_up", "replay", "host", "plugin", "delegation"]
ObservationName = Literal[
    "runtime_event",
    "context_prepared",
    "tool_call_requested",
    "tool_call_finished",
    "run_finished",
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
    """Resolve a declared slot from its user-selected environment or saved-key binding."""

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
    """Generation context; configuration objects are read-only mappings and arrays are tuples."""

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
    environment: PluginEnvironment | None = None
    services: PluginServices | None = None
    processes: PluginProcesses | None = None
    models: PluginModels | None = None
    tools: RunTools | None = None
    agents: RunAgents | None = None
    tool_groups: PluginToolGroups | None = None
    scheduler: PluginScheduler | None = None


class CancellationContext(Protocol):
    """Host-owned cooperative cancellation services for in-process callbacks."""

    @property
    def is_aborted(self) -> bool: ...

    @property
    def abort_reason(self) -> str: ...

    def raise_if_aborted(self) -> None: ...

    async def sleep(self, seconds: float) -> None: ...


class ToolExecutionContext(CancellationContext, Protocol):
    """Host-owned, request-scoped services available to an in-process tool.

    Worker tools receive the same workspace fields in ``ToolCall`` and are
    cancelled through the worker protocol's ``cancel`` notification.
    """

    @property
    def workspace(self) -> str | None: ...

    @property
    def workspace_only(self) -> bool: ...

    def resolve_path(self, path: str) -> str: ...

    def resolve_working_directory(self, cwd: str | None = None) -> str | None: ...


@dataclass(frozen=True)
class ToolCall:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    tool_call_id: str
    session_id: str
    run_id: str
    tool_name: str
    arguments: Mapping[str, object]
    workspace: str | None = None
    workspace_only: bool = False


@dataclass(frozen=True)
class ToolOutput:
    content: ToolContent = ""
    structured: Mapping[str, object] = field(default_factory=dict)
    resource_refs: tuple[str, ...] = ()
    is_error: bool = False
    session_control_transitions: tuple[SessionControlTransition, ...] = ()


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
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
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
    """Request a provenance-bearing input through the Host runtime.

    Model role, scaffold origin and semantic labeling are independent. The
    producer declares each field; the Host preserves them without inference.
    ``driver_id`` selects the algorithm of a follow-up Run at input acceptance.
    """

    prompt: str
    mode: SessionInputMode = "follow_up"
    title: str | None = None
    role: SessionInputRole = "system"
    scaffold: bool = True
    semantic_tag: str | None = None
    driver_id: str | None = None

    def __post_init__(self) -> None:
        if self.driver_id is not None and (not self.driver_id.strip() or self.mode != "follow_up"):
            raise ValueError("Driver selection requires a nonempty identity and follow-up input")


@dataclass(frozen=True)
class SessionInputBatch:
    """Submit one non-empty, ordered group of inputs as a single delivery unit.

    Every input must use the same delivery mode and Driver selection.
    ``coalesce_key`` may replace pending projections owned by the same command.
    """

    inputs: tuple[SessionInput, ...]
    coalesce_key: str | None = None

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ValueError("session input batch cannot be empty")
        if len({item.mode for item in self.inputs}) != 1:
            raise ValueError("all inputs in a session input batch must use the same mode")
        if len({item.driver_id for item in self.inputs}) != 1:
            raise ValueError("all inputs in a session input batch must select the same Driver")
        if self.coalesce_key is not None and not self.coalesce_key.strip():
            raise ValueError("session input batch coalesce key cannot be blank")


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
    active_when: str | None = None
    interaction_controller: str | None = None


@dataclass(frozen=True)
class ToolGroupContribution:
    """One complete tool group; revision identifies its implementation semantics."""

    id: str
    revision: str
    tools: tuple[ToolContribution, ...] = ()


class PluginToolGroups(Protocol):
    async def publish(self, group: ToolGroupContribution) -> Registration: ...


ToolPolicyAction = Literal["ignore", "require_approval", "deny"]


@dataclass(frozen=True)
class ToolPolicyCall:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
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
    active_when: str | None = None


PromptPolicyAction = Literal["allow", "deny"]


@dataclass(frozen=True)
class PromptAttachmentMetadata:
    resource_id: str | None
    media_type: str | None
    size: int | None
    kind: Literal["upload", "reference"] = "upload"
    name: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class PromptSkillActivationMetadata:
    name: str
    source: Literal["user", "workspace", "plugin"]
    location: str
    content_digest: str


@dataclass(frozen=True)
class PromptPolicyCall:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    prospective_run_id: str
    prompt: str
    input_source: PromptInputSource = "user"
    workspace: str | None = None
    attachments: tuple[PromptAttachmentMetadata, ...] = ()
    catalog_id: str | None = None
    execution_contracts: Mapping[str, str] = field(default_factory=dict)
    skill_activation: PromptSkillActivationMetadata | None = None


@dataclass(frozen=True)
class PromptPolicyDecision:
    action: PromptPolicyAction = "allow"
    reason: str | None = None


PromptPolicyHandler = Callable[
    [PromptPolicyCall],
    PromptPolicyDecision | Awaitable[PromptPolicyDecision],
]


@dataclass(frozen=True)
class PromptPolicyContribution:
    id: str
    handler: PromptPolicyHandler
    priority: int = 100
    timeout_seconds: float = 5.0
    active_when: str | None = None


TurnPolicyAction = Literal["allow", "continue"]


@dataclass(frozen=True)
class TurnCompletionCall:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
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
    active_when: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    status: RunStatus
    origin: RunOrigin
    final_text: str = ""
    error: str | None = None
    usage: Mapping[str, int | float] = field(default_factory=dict)
    # Native execution facts; None means this observation did not supply the fact.
    tool_call_count: int | None = None
    automatic_continuation: bool | None = None


@dataclass(frozen=True)
class RuntimeEventView:
    event_id: str
    session_id: str
    run_id: str | None
    turn_id: str | None
    event_type: str
    created_at: datetime
    sequence: int
    history_epoch: int = 0
    payload: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeEventObservation:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str | None
    record: RuntimeEventView
    event: Literal["runtime_event"] = field(init=False, default="runtime_event")


@dataclass(frozen=True)
class ContextPreparedObservation:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    message_count: int
    tool_count: int
    model_round: int
    context_metadata: Mapping[str, object] = field(default_factory=dict)
    event: Literal["context_prepared"] = field(init=False, default="context_prepared")


@dataclass(frozen=True)
class ToolCallRequestedObservation:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    canonical_tool_id: str
    owner_id: str
    effect: ToolEffect
    batch_behavior: ToolBatchBehavior
    arguments: Mapping[str, object] = field(default_factory=dict)
    event: Literal["tool_call_requested"] = field(
        init=False,
        default="tool_call_requested",
    )


@dataclass(frozen=True)
class ToolCallFinishedObservation:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    canonical_tool_id: str
    owner_id: str
    effect: ToolEffect
    batch_behavior: ToolBatchBehavior
    is_error: bool
    content: str
    structured: Mapping[str, object] = field(default_factory=dict)
    resource_refs: tuple[str, ...] = ()
    exit_code: int | None = None
    event: Literal["tool_call_finished"] = field(init=False, default="tool_call_finished")


@dataclass(frozen=True)
class RunFinishedObservation:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    outcome: RunOutcome
    event: Literal["run_finished"] = field(init=False, default="run_finished")


type Observation = (
    RuntimeEventObservation
    | ContextPreparedObservation
    | ToolCallRequestedObservation
    | ToolCallFinishedObservation
    | RunFinishedObservation
)
ObserverHandler = Callable[[Observation], None | Awaitable[None]]


@dataclass(frozen=True)
class ObserverContribution:
    id: str
    event: ObservationName
    handler: ObserverHandler
    priority: int = 100
    timeout_seconds: float = 1.0
    event_types: tuple[str, ...] = ()
    active_when: str | None = None


@dataclass(frozen=True)
class CommandCall:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    command_id: str
    argument: str = ""
    session_id: str | None = None
    workspace: str | None = None


@dataclass(frozen=True)
class CommandOutput:
    """Command effects; run_action controls existing work without adding model input."""

    output: str = ""
    data: Mapping[str, object] = field(default_factory=dict)
    session_inputs: tuple[SessionInput, ...] = ()
    session_input_batches: tuple[SessionInputBatch, ...] = ()
    session_control_transitions: tuple[SessionControlTransition, ...] = ()
    run_action: Literal["stop", "continue"] | None = None


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
    role: SessionInputRole = "system"
    scaffold: bool = True
    semantic_tag: str | None = None
    coalesce_key: str | None = None
    reason: str | None = None
    session_control_transitions: tuple[SessionControlTransition, ...] = ()


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
    active_when: str | None = None


@dataclass(frozen=True)
class CommandContribution:
    id: str
    description: str
    handler: CommandHandler
    usage: str = ""
    timeout_seconds: float = 5.0


SessionControlAccess = Literal["exclusive", "shared"]
SessionControlTransitionAction = Literal["acquire", "release", "replace"]


@dataclass(frozen=True)
class SessionControlClaim:
    domain: str
    access: SessionControlAccess = "exclusive"


@dataclass(frozen=True)
class SessionControlTransition:
    action: SessionControlTransitionAction
    controller_id: str | None = None
    activation_id: str | None = None
    replace_activation_ids: tuple[str, ...] = ()
    expected_session_revision: int | None = None
    expected_activation_revision: int | None = None
    payload: JsonValue = None
    reason: str | None = None


@dataclass(frozen=True)
class SessionControllerCall:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    session_kind: SessionKind
    activation_id: str
    controller_id: str
    action: Literal["activate", "deactivate"]
    payload: JsonValue = None
    reason: str | None = None
    workspace: str | None = None
    replacement_controller_id: str | None = None


@dataclass(frozen=True)
class SessionControllerOutput:
    output: str = ""
    data: Mapping[str, object] = field(default_factory=dict)
    usage_scope: str | None = None


SessionControllerHandler = Callable[
    [SessionControllerCall],
    SessionControllerOutput | Awaitable[SessionControllerOutput],
]


@dataclass(frozen=True)
class SessionControllerContribution:
    id: str
    description: str
    claims: tuple[SessionControlClaim, ...]
    activate: SessionControllerHandler
    deactivate: SessionControllerHandler
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class ContextCall:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str
    message_count: int
    model_round: int
    workspace: str | None = None


@dataclass(frozen=True)
class ContextOutput:
    fragments: tuple[str | ContextFragment, ...] = ()


@dataclass(frozen=True)
class ContextFragment:
    """Model context supplied for one request or persisted as a canonical message.

    Supply raw ``text`` and independently declare its message role, scaffold
    origin and optional semantic label. The Host validates and renders a label
    only when ``semantic_tag`` is present.
    """

    text: str
    persistence: ContextFragmentPersistence = "ephemeral"
    target: ContextFragmentTarget = "message"
    role: SessionInputRole = "system"
    scaffold: bool = True
    semantic_tag: str | None = None


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
    active_when: str | None = None


@dataclass(frozen=True)
class CompactionCall:
    session_state: PluginSessionStateView | None = field(default=None, kw_only=True)
    session_id: str
    run_id: str | None
    previous_summary: str | None
    messages: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class CompactionOutput:
    summary: str
    usage: Mapping[str, int | float] = field(default_factory=dict)


CompactionHandlerResult = CompactionOutput | Awaitable[CompactionOutput]
CompactionHandler = (
    Callable[[CompactionCall], CompactionHandlerResult]
    | Callable[[CompactionCall, CancellationContext], CompactionHandlerResult]
)

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
    tool_groups: list[ToolGroupContribution] = field(default_factory=list)
    services: list[ServiceContribution] = field(default_factory=list)
    tools: list[ToolContribution] = field(default_factory=list)
    observers: list[ObserverContribution] = field(default_factory=list)
    commands: list[CommandContribution] = field(default_factory=list)
    context_providers: list[ContextContribution] = field(default_factory=list)
    compaction_strategies: list[CompactionContribution] = field(default_factory=list)
    background_services: list[BackgroundServiceContribution] = field(default_factory=list)
    providers: list[ProviderContribution] = field(default_factory=list)
    run_drivers: list[RunDriverContribution] = field(default_factory=list)
    continuation_policies: list[ContinuationPolicyContribution] = field(default_factory=list)
    prompt_policies: list[PromptPolicyContribution] = field(default_factory=list)
    turn_policies: list[TurnPolicyContribution] = field(default_factory=list)
    tool_policies: list[ToolPolicyContribution] = field(default_factory=list)
    session_controllers: list[SessionControllerContribution] = field(default_factory=list)


class PluginInstance(Protocol):
    async def prepare(self) -> ContributionSet: ...

    async def start(self) -> None: ...

    async def stop(self, reason: StopReason, deadline: float) -> None: ...


PluginFactory = Callable[[PluginContext], PluginInstance | Awaitable[PluginInstance]]


__all__ = [
    "RunInput",
    "RunInputs",
    "PluginToolGroups",
    "ToolGroupContribution",
    "Registration",
    "AgentTask",
    "RunAgents",
    "RunDriverContribution",
    "RunDriverContext",
    "DriverResume",
    "DriverFinished",
    "DriverSuspended",
    "ModelResponse",
    "ModelToolCall",
    "NativeToolResult",
    "ToolBatchResult",
    "RunModels",
    "PluginModels",
    "RunTools",
    "RunToolCall",
    "AnswerResolution",
    "BackgroundServiceContribution",
    "CommandCall",
    "CommandContribution",
    "CommandOutput",
    "CancellationContext",
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
    "ContextFragmentTarget",
    "ContextPreparedObservation",
    "ContextOutput",
    "ContributionSet",
    "DiagnosticEmitter",
    "JsonValue",
    "Observation",
    "ObservationName",
    "ObserverContribution",
    "PluginContext",
    "PluginEnvironment",
    "PluginProcesses",
    "PluginServices",
    "PluginScheduler",
    "PluginCredentialResolver",
    "PluginDataStore",
    "PluginFactory",
    "PluginIdentity",
    "PluginInstance",
    "PluginLogger",
    "PluginProvenanceSummary",
    "PluginTaskGroup",
    "PluginSessionState",
    "PluginSessionControl",
    "PluginRunControl",
    "PluginSessionStateView",
    "PromptAttachmentMetadata",
    "PromptInputSource",
    "PromptPolicyAction",
    "PromptPolicyCall",
    "PromptPolicyContribution",
    "PromptPolicyDecision",
    "PromptSkillActivationMetadata",
    "RunOutcome",
    "RunFinishedObservation",
    "RunOrigin",
    "RunStatus",
    "SessionControlAccess",
    "ServiceContribution",
    "SessionControlClaim",
    "SessionControlTransition",
    "SessionControlTransitionAction",
    "SessionControllerCall",
    "SessionControllerContribution",
    "SessionControllerHandler",
    "SessionControllerOutput",
    "SessionInput",
    "SessionInputBatch",
    "SessionInputMode",
    "SessionInputRole",
    "SessionKind",
    "StopReason",
    "ToolCall",
    "ToolCallFinishedObservation",
    "ToolCallRequestedObservation",
    "ToolBatchBehavior",
    "ToolContribution",
    "ToolEffect",
    "ToolExecutionContext",
    "ToolInputRequest",
    "ToolInputResolution",
    "ToolOutput",
    "ToolContent",
    "ToolTextContent",
    "ToolImageContent",
    "ToolAudioContent",
    "ToolVideoContent",
    "ToolPolicyAction",
    "ToolPolicyCall",
    "ToolPolicyContribution",
    "ToolPolicyDecision",
    "RuntimeEventObservation",
    "RuntimeEventView",
    "ToolPermissionControl",
    "ToolResumeCall",
    "UnattendedResolution",
    "TurnCompletionCall",
    "TurnPolicyAction",
    "TurnPolicyContribution",
    "TurnPolicyDecision",
]
