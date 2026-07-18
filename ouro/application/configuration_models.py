from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.config.provider_models import ProviderProtocol, ProviderType
from ouro.config.reasoning import EndpointReasoningConfig
from ouro.core.provider import ProviderRetryPolicy
from ouro.core.reasoning import ReasoningRequest
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode

ConfigurationSection = Literal[
    "providers",
    "prompts",
    "permissions",
    "tool_execution",
    "queued_inputs",
    "run_budget",
    "compaction",
    "skills",
    "mcp",
]
ConfigurationEffectiveFrom = Literal["next_request", "next_runtime"]
CredentialState = Literal["not_required", "configured", "missing"]
MCPConnectionStatus = Literal[
    "configured",
    "starting",
    "connected",
    "reconnecting",
    "failed",
    "stopping",
    "stopped",
    "disabled",
]


class ConfigurationChange(ApplicationDTO):
    section: ConfigurationSection
    action: str
    effective_from: ConfigurationEffectiveFrom
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class CredentialStatus(ApplicationDTO):
    reference: str
    state: CredentialState
    configured: bool
    available: bool


class ProviderEndpointView(ApplicationDTO):
    endpoint_id: str
    model_id: str
    base_url: str | None = None
    api_path: str | None = None
    temperature: float | None = None
    context_window: int
    max_output_tokens: int
    supports_tools: bool
    supports_vision: bool
    supports_streaming: bool
    supports_structured_output: bool
    reasoning: EndpointReasoningConfig = Field(default_factory=EndpointReasoningConfig)
    request_options: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    default: bool = False


class ProviderView(ApplicationDTO):
    provider_id: str
    provider_type: ProviderType
    protocol: ProviderProtocol
    credential: CredentialStatus
    enabled: bool
    timeout: float
    retry_policy: ProviderRetryPolicy
    base_url: str | None = None
    api_path: str | None = None
    default: bool = False
    default_endpoint: str | None = None
    endpoints: list[ProviderEndpointView] = Field(default_factory=list)

    @property
    def endpoint_count(self) -> int:
        return len(self.endpoints)


class ProviderCatalogView(ApplicationDTO):
    config_path: str
    default_provider_id: str | None = None
    providers: list[ProviderView] = Field(default_factory=list)
    change: ConfigurationChange | None = None


class ProviderEndpointCatalogView(ApplicationDTO):
    provider_id: str
    default_endpoint: str | None = None
    endpoints: list[ProviderEndpointView] = Field(default_factory=list)
    change: ConfigurationChange | None = None


class ProviderTemplateView(ApplicationDTO):
    template_id: str
    display_name: str
    description: str
    provider_id: str
    provider_type: ProviderType
    protocol: ProviderProtocol
    credential_env: str
    base_url: str
    api_path: str
    default_endpoint: str
    endpoints: list[ProviderEndpointView] = Field(default_factory=list)
    documentation_url: str
    verified_on: str

    @property
    def endpoint_count(self) -> int:
        return len(self.endpoints)


class ProviderTemplateCatalogView(ApplicationDTO):
    templates: list[ProviderTemplateView] = Field(default_factory=list)


class SystemPromptConfigView(ApplicationDTO):
    config_path: str
    configured: bool
    system_prompt: str | None = None
    effective_system_prompt: str
    change: ConfigurationChange | None = None


class PermissionConfigView(ApplicationDTO):
    config_path: str
    tools: dict[str, PermissionMode] = Field(default_factory=dict)
    change: ConfigurationChange | None = None


class ToolExecutionConfigView(ApplicationDTO):
    config_path: str
    mode: ToolExecutionMode
    max_concurrency: int
    workspace_only: bool
    max_output_chars: int
    change: ConfigurationChange | None = None


class QueuedInputConfigView(ApplicationDTO):
    config_path: str
    consumption_mode: Literal["one_by_one", "all_at_once"]
    change: ConfigurationChange | None = None


class RunBudgetConfigView(ApplicationDTO):
    config_path: str
    max_model_rounds: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_elapsed_seconds: float | None = None
    change: ConfigurationChange | None = None


class CompactionModelConfigView(ApplicationDTO):
    provider_id: str | None = None
    endpoint_id: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    reasoning: ReasoningRequest | None = None

    @property
    def has_dedicated_endpoint(self) -> bool:
        return self.provider_id is not None and self.endpoint_id is not None


class CompactionConfigView(ApplicationDTO):
    config_path: str
    strategy_id: str
    fallback_strategy_id: str | None = None
    policy: dict[str, Any] = Field(default_factory=dict)
    model: CompactionModelConfigView
    change: ConfigurationChange | None = None


class MCPServerConfigView(ApplicationDTO):
    server_id: str
    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    inherit_env: list[str] = Field(default_factory=list)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    credential_refs: dict[str, str] = Field(default_factory=dict)
    enabled: bool
    auto_start: bool
    auto_reconnect: bool
    max_reconnect_attempts: int
    reconnect_backoff_seconds: float
    timeout_seconds: float
    health_check_interval_seconds: float
    permission_mode: PermissionMode
    tool_effect_overrides: dict[str, Literal["read_only", "state_changing", "unknown"]] = Field(
        default_factory=dict
    )


class MCPConfigCatalogView(ApplicationDTO):
    config_path: str
    servers: list[MCPServerConfigView] = Field(default_factory=list)
    change: ConfigurationChange | None = None


class MCPConnectionCheckView(ApplicationDTO):
    ok: bool
    status: MCPConnectionStatus
    server: MCPServerConfigView
    server_info: dict[str, Any] | None = None
    tool_names: tuple[str, ...] = ()
    error: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


class ManagedSkillView(ApplicationDTO):
    name: str
    source: str
    scope: Literal["user", "workspace"]
    target: str
    selector: str | None = None
    ref: str | None = None


class SkillPolicyView(ApplicationDTO):
    config_path: str
    enabled: bool
    disabled: list[str] = Field(default_factory=list)
    paths: list[dict[str, str]] = Field(default_factory=list)
    scan_agents: bool
    scan_claude: bool
    scan_copilot: bool
    scan_codex: bool
    scan_github: bool
    max_depth: int
    max_directories: int
    installed: list[ManagedSkillView] = Field(default_factory=list)
    change: ConfigurationChange | None = None


class SkillRootView(ApplicationDTO):
    path: str
    source: Literal["user", "workspace", "plugin"]
    origin: str
    exists: bool


class SkillDiagnosticView(ApplicationDTO):
    level: Literal["warning", "error"]
    code: str
    message: str
    path: str


class SkillView(ApplicationDTO):
    name: str
    description: str
    location: str
    source: Literal["user", "workspace", "plugin"]
    enabled: bool
    managed: bool = False
    install: ManagedSkillView | None = None
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: str | None = None
    user_invocable: bool = True
    disable_model_invocation: bool = False
    argument_hint: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class LoadedSkillView(ApplicationDTO):
    skill: SkillView
    content: str
    resources: list[str] = Field(default_factory=list)


class SkillCatalogView(ApplicationDTO):
    roots: list[SkillRootView] = Field(default_factory=list)
    skills: list[SkillView] = Field(default_factory=list)
    diagnostics: list[SkillDiagnosticView] = Field(default_factory=list)


class SkillValidationView(ApplicationDTO):
    valid: bool
    skills: list[SkillView] = Field(default_factory=list)
    diagnostics: list[SkillDiagnosticView] = Field(default_factory=list)


class ManagedSkillResultView(ApplicationDTO):
    name: str
    target: str
    source: str
    scope: Literal["user", "workspace"]


class SkillManagementView(ApplicationDTO):
    action: str
    skills: list[ManagedSkillResultView] = Field(default_factory=list)
    diagnostics: list[SkillDiagnosticView] = Field(default_factory=list)
    change: ConfigurationChange


class SkillsPromptView(ApplicationDTO):
    skill_count: int
    prompt: str


class ConfigurationOverview(ApplicationDTO):
    config_path: str
    providers: ProviderCatalogView
    prompt: SystemPromptConfigView
    permissions: PermissionConfigView
    tool_execution: ToolExecutionConfigView
    queued_inputs: QueuedInputConfigView
    run_budget: RunBudgetConfigView
    compaction: CompactionConfigView
    skills: SkillPolicyView
    mcp: MCPConfigCatalogView
