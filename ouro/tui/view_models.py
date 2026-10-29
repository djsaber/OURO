"""Terminal Host records independent of controller implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, cast, get_args

from ouro.application.contracts.base import ApplicationStartupReport
from ouro.application.contracts.configuration import MCPTransportInput, SkillInstallScopeInput
from ouro.application.contracts.models import AvailableModel, ModelReasoningState
from ouro.application.contracts.plugins import PluginCommandExecutionView
from ouro.application.contracts.sessions import (
    SessionCatalogEntry,
    SessionContextStatus,
    SessionDetail,
    SessionView,
)
from ouro.application.contracts.tool_activity import PendingInteraction, RuntimeToolCallActivity
from ouro.application.inputs.queue import QueuedUserInputView
from ouro.application.observations.sessions import SessionObservation
from ouro.config import EndpointReasoningConfig, ProviderProtocol, ProviderType
from ouro.core.agents import ExecutionMode, ToolSelection
from ouro.core.approvals import ToolPermissionControl
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.reasoning import ReasoningRequest
from ouro.core.results import RunResult
from ouro.core.state import RunRecord
from ouro.core.tasks import TaskCounts
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode, ToolEffect, ToolSource

EventPublisher = Callable[[EventRecord], None]


LifecyclePublisher = Callable[["TuiLifecycleStatus"], None]


TuiApprovalPreset = Literal["inherit", "auto", "approval", "deny", "afk"]


TUI_APPROVAL_PRESETS = cast(tuple[TuiApprovalPreset, ...], get_args(TuiApprovalPreset))


@dataclass(frozen=True)
class TuiLifecycleStatus:
    activity: str
    detail: str


@dataclass(frozen=True)
class McpServerStatus:
    server_id: str
    name: str
    transport: str
    status: str
    tool_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class TuiDailyUsage:
    date: str
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class TuiSystemStatus:
    workspace: str | None = None
    model_names: tuple[str, ...] = ()
    model_count: int = 0
    skill_count: int = 0
    skill_names: tuple[str, ...] = ()
    plugin_active_count: int = 0
    plugin_total_count: int = 0
    plugin_issue_count: int = 0
    plugin_active_names: tuple[str, ...] = ()
    mcp_connected_count: int = 0
    mcp_total_count: int = 0
    mcp_tool_count: int = 0
    mcp_connecting_count: int = 0
    mcp_failed_count: int = 0
    usage_request_count: int = 0
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_cache_read_tokens: int = 0
    usage_daily: tuple[TuiDailyUsage, ...] = ()
    task_counts: TaskCounts = field(default_factory=TaskCounts)
    task_error: str | None = None


@dataclass(frozen=True)
class TuiProviderConfiguration:
    """Supplier-level configuration intent collected by the TUI."""

    display_name: str
    provider_type: ProviderType
    base_url: str | None
    api_path: str | None
    api_key: str | None
    api_key_env: str | None
    timeout: float
    headers: dict[str, object] | None = field(default=None, repr=False)
    provider_options: dict[str, object] | None = None
    enabled: bool = True
    make_default: bool = False
    auth_driver: str | None = None
    protocol: ProviderProtocol | None = None


@dataclass(frozen=True)
class TuiEndpointConfiguration:
    """One provider-owned callable endpoint collected by the TUI."""

    display_name: str | None
    model_id: str
    context_window: int
    max_output_tokens: int | None
    supports_tools: bool = True
    supports_image_input: bool = False
    supports_audio_input: bool = False
    supports_video_input: bool = False
    supports_streaming: bool = True
    supports_structured_output: bool = True
    reasoning: EndpointReasoningConfig | None = None
    request_options: dict[str, object] | None = None


@dataclass(frozen=True)
class TuiToolConfiguration:
    name: str
    display_name: str
    source: ToolSource | Literal["unavailable"]
    effect: ToolEffect
    default_permission: PermissionMode | None
    plugin_id: str | None
    plugin_name: str | None
    plugin_permission_override: PermissionMode | None
    permission_override: PermissionMode | None
    permission_control: ToolPermissionControl
    available: bool = True

    @property
    def inherited_permission(self) -> PermissionMode | None:
        if self.permission_control == "tool":
            return self.default_permission
        return self.plugin_permission_override or self.default_permission

    @property
    def effective_permission(self) -> PermissionMode | None:
        if self.permission_control == "tool":
            return self.default_permission
        return self.permission_override or self.inherited_permission


@dataclass(frozen=True)
class TuiToolExecutionConfiguration:
    mode: ToolExecutionMode
    max_concurrency: int
    workspace_only: bool
    max_output_chars: int


@dataclass(frozen=True)
class TuiAgentRuntimeConfiguration:
    expected_revision: int
    allowed_agent_id_patterns: tuple[str, ...]
    allowed_model_patterns: tuple[str, ...]
    allowed_execution_modes: tuple[ExecutionMode, ...]
    allow_write: bool
    allow_network: bool
    allow_mcp: bool
    allow_external_paths: bool


@dataclass(frozen=True)
class TuiAgentDefinitionConfiguration:
    agent_id: str
    name: str
    description: str
    system_prompt: str
    tool_selection: ToolSelection
    model: str | None


@dataclass(frozen=True)
class TuiMCPServerConfiguration:
    server_id: str | None
    name: str
    transport: MCPTransportInput
    target: str | None
    args: list[str] | None
    env: dict[str, str] | None
    inherit_env: list[str] | None
    cwd: str | None
    headers: dict[str, str] | None
    credential_refs: dict[str, str] | None
    enabled: bool
    auto_start: bool
    auto_reconnect: bool
    max_reconnect_attempts: int
    reconnect_backoff_seconds: float
    timeout_seconds: float
    health_check_interval_seconds: float
    permission_mode: PermissionMode
    tool_effect_overrides: dict[str, ToolEffect] | None


@dataclass(frozen=True)
class TuiSkillInstallConfiguration:
    source: str
    scope: SkillInstallScopeInput
    selector: str | None = None
    install_all: bool = False
    force: bool = False
    ref: str | None = None


@dataclass(frozen=True)
class TuiPluginInstallConfiguration:
    source: str
    ref: str | None = None
    subdirectory: str | None = None


@dataclass(frozen=True)
class TuiSkillInvocationOption:
    name: str
    description: str
    argument_hint: str | None = None


@dataclass(frozen=True)
class TuiCompactionConfiguration:
    strategy_id: str
    fallback_strategy_id: str | None
    auto_enabled: bool
    trigger_ratio: float
    target_ratio: float
    model_key: str | None
    temperature: float | None
    max_output_tokens: int | None
    reasoning: ReasoningRequest | None


@dataclass(frozen=True)
class TuiSessionControlStatus:
    controller_id: str
    label: str


@dataclass(frozen=True)
class TuiPluginCommandExecution:
    execution: PluginCommandExecutionView
    run_results: tuple[RunResult, ...] = ()


@dataclass(frozen=True)
class TuiBootstrap:
    models: tuple[AvailableModel, ...]
    current_model: AvailableModel
    sessions: tuple[SessionCatalogEntry, ...]
    startup_report: ApplicationStartupReport
    mcp_servers: tuple[McpServerStatus, ...] = ()
    system_status: TuiSystemStatus = TuiSystemStatus()


@dataclass(frozen=True)
class LoadedSession:
    detail: SessionDetail
    messages: tuple[MessageRecord, ...]
    events: tuple[EventRecord, ...]
    runs: tuple[RunRecord, ...] = ()
    active_tool_calls: tuple[RuntimeToolCallActivity, ...] = ()
    pending_interactions: tuple[PendingInteraction, ...] = ()
    current_request: ModelRequestHeader | None = None
    context_status: SessionContextStatus | None = None
    effective_context_metrics: ContextMetrics | None = None
    compactions: tuple[CompactionRecord, ...] = ()
    queued_inputs: tuple[QueuedUserInputView, ...] = ()
    session_controls: tuple[TuiSessionControlStatus, ...] = ()
    history_before_cursor: int | None = None
    approval_preset: TuiApprovalPreset = "inherit"
    reasoning_state: ModelReasoningState | None = None


@dataclass(frozen=True)
class TuiSessionObservation:
    loaded: LoadedSession
    subscription: SessionObservation
    control_session: SessionView


@dataclass(frozen=True)
class TuiNewSession:
    model: AvailableModel
    initial_model: str | None
    workspace: str | None
