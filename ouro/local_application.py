from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.agents import AgentService
from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import MCPRuntimeServerView
from ouro.application.data_transfer import (
    DataKind,
    DataTransferItem,
    DataTransferRequest,
    DataTransferResult,
    DataTransferService,
)
from ouro.application.dto import ApplicationStartupReport, Diagnostic
from ouro.application.events import RuntimeEventSink
from ouro.application.model_generation import ModelGenerationService
from ouro.application.models import AdapterBuilder, ConfiguredModelCatalog
from ouro.application.plugins import PluginService
from ouro.application.provider_auth import ProviderAuthService
from ouro.application.resources import ResourceService
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_configuration import (
    RuntimeConfigurationResolver,
    RuntimeProviderOptions,
)
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.session_transfer import SessionTransferService
from ouro.application.sessions import SessionService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.application.task_execution import TaskExecutionCoordinator
from ouro.application.task_scheduler import TaskScheduler
from ouro.application.tasks import TaskService
from ouro.application.usage import UsageService
from ouro.auth.credentials import CredentialService
from ouro.auth.driver_contracts import AuthDriver
from ouro.auth.drivers.openai_codex import OpenAICodexDriver
from ouro.auth.managed_requests import ManagedRequestAuthenticator, RequestTarget
from ouro.auth.store import FileCredentialStore
from ouro.automation.local_files import LocalTaskRepository
from ouro.automation.sqlite import SQLiteTaskRepository
from ouro.billing import LocalUsageLedger
from ouro.builtin_plugins import LOCAL_BUILTIN_PLUGINS
from ouro.config import CompactionConfig, CredentialRef, CredentialResolver
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.config.provider_models import SUPPORTED_PROVIDER_TYPES, provider_type_spec
from ouro.config.providers import ProviderConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import settle_owned_tasks
from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.context import ContextBuilder
from ouro.core.context_providers import ContextProviderRegistry
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.errors import ConfigError, OuroError
from ouro.core.observers import ObserverRegistry
from ouro.core.prompt_policies import PromptPolicyRegistry
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.turn_policies import TurnPolicyRegistry
from ouro.deployment import (
    SessionBackend,
    TaskBackend,
    ensure_home_available,
    resolve_session_backend,
    resolve_task_backend,
)
from ouro.local_providers import build_plugin_provider
from ouro.mcp import MCPRuntimeResource, MCPServerConfig, MCPServerRecord
from ouro.plugins.contributions import ContributionCoordinator
from ouro.plugins.providers import ProviderBinder, ProviderBinding
from ouro.plugins.registry import PluginCommandRegistry
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.providers import build_runtime_adapter
from ouro.providers.authentication import ProviderRequestPolicy
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore, SessionStore, SQLiteSessionStore
from ouro.skills import SkillMetadata
from ouro.tools import ToolRegistry, builtin_tools


@dataclass(frozen=True)
class LocalServices:
    """Complete local application facets for Hosts that do not need a Runtime."""

    sessions: SessionService
    resources: ResourceService
    usage: UsageService
    configuration: ConfigurationFacade
    plugins: PluginService


@dataclass(frozen=True)
class LocalSessionServices:
    """Session and resource facets sharing one canonical Session repository."""

    sessions: SessionService
    resources: ResourceService
    _store: SessionStore = field(repr=False)


@dataclass(frozen=True)
class LocalAgentDefinitionServices:
    """Definition management facet with the same live Tool catalog as a local Runtime."""

    definitions: AgentDefinitionService
    tools: ToolRegistry


@dataclass(frozen=True)
class ProviderServicePolicy:
    """Trusted service registration, never loaded from user request_options."""

    policy_id: str
    targets: tuple[RequestTarget, ...]
    requests: ProviderRequestPolicy = ProviderRequestPolicy()

    def __post_init__(self) -> None:
        if not self.policy_id or not self.targets:
            raise ValueError("service registration requires an id and allowed targets")


_BUILTIN_SERVICE_POLICIES = (
    ProviderServicePolicy(
        "openai-codex",
        (RequestTarget("https://chatgpt.com", "/backend-api/codex/responses"),),
        ProviderRequestPolicy(billing_mode="subscription", supports_output_limit=False),
    ),
)


def _local_auth_drivers(additional: tuple[AuthDriver, ...]) -> tuple[AuthDriver, ...]:
    # Public client used by the verified upstream Codex compatibility flow.
    # No clients, secrets or account entitlement are resolved at construction.
    return (OpenAICodexDriver(client_id="app_EMoamEEZ73f0CkXaXp7hrann"), *additional)


class LocalApplication:
    """Own one local OURO runtime and all process-scoped resources around it."""

    def __init__(
        self,
        runtime: RuntimeService,
        mcp: MCPRuntimeResource,
        *,
        plugin_runtime: PluginRuntimeResource | None = None,
        plugins: PluginService | None = None,
        supervisor: RuntimeSupervisor | None = None,
        agent_definitions: AgentDefinitionService | None = None,
        agents: AgentService | None = None,
        tasks: TaskService | None = None,
        task_scheduler: TaskScheduler | None = None,
    ) -> None:
        self.runtime = runtime
        self.provider_auth = runtime.provider_auth
        self.mcp = mcp
        self.plugin_runtime = plugin_runtime
        self.plugins = plugins
        self.supervisor = supervisor or RuntimeSupervisor(runtime)
        self.agent_definitions = agent_definitions
        self.agents = agents
        self.tasks = tasks
        self.task_scheduler = task_scheduler
        self._startup_report = ApplicationStartupReport()
        self._runtime_startup_diagnostics: tuple[Diagnostic, ...] = ()
        self._agent_startup_diagnostics: tuple[Diagnostic, ...] = ()
        self._started = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def startup_report(self) -> ApplicationStartupReport:
        return self._startup_report

    def transfer_data(self, request: DataTransferRequest) -> DataTransferResult:
        return self._data_transfer_service().transfer(request)

    def data_archive_kind(self, path: Path) -> DataKind:
        return self._data_transfer_service().archive_kind(path)

    def data_transfer_items(self, request: DataTransferRequest) -> tuple[DataTransferItem, ...]:
        return self._data_transfer_service().selection_items(request)

    def _data_transfer_service(self) -> DataTransferService:
        home = self.runtime.environment.home
        if home is None:
            raise OuroError("runtime has no configured deployment home")
        return build_local_data_transfer_service(home, session_store=self.runtime.store)

    @classmethod
    def build(
        cls,
        home: str | Path | None = None,
        *,
        provider_options: RuntimeProviderOptions | None = None,
        system_prompt: str | None = None,
        event_sink: RuntimeEventSink | None = None,
        session_backend: SessionBackend | None = None,
        task_backend: TaskBackend | None = None,
        auth_drivers: tuple[AuthDriver, ...] = (),
        service_policies: tuple[ProviderServicePolicy, ...] = (),
    ) -> LocalApplication:
        task_backend = resolve_task_backend(task_backend)
        runtime = build_local_runtime(
            home,
            provider_options=provider_options,
            system_prompt=system_prompt,
            event_sink=event_sink,
            session_backend=session_backend,
            auth_drivers=auth_drivers,
            service_policies=service_policies,
        )
        return cls.from_runtime(runtime, task_backend=task_backend)

    @classmethod
    def from_runtime(
        cls,
        runtime: RuntimeService,
        *,
        home: str | Path | None = None,
        task_backend: TaskBackend | None = None,
    ) -> LocalApplication:
        task_backend = resolve_task_backend(task_backend)
        configured_home = runtime.environment.home
        if configured_home is None and home is not None:
            configured_home = resolve_local_home(home)
        if configured_home is None:
            raise OuroError("runtime has no configured deployment home")
        ensure_home_available(configured_home)
        plugin_runtime, plugins = _build_local_plugin_runtime(
            configured_home,
            tools=runtime.tools,
            observers=runtime.observers,
            context_providers=runtime.context_providers,
            compaction=runtime.compaction_strategies,
            continuations=runtime.continuations,
            prompt_policies=runtime.prompt_policies,
            turn_policies=runtime.turn_policies,
            tool_policies=runtime.tool_policies,
            skills=runtime.skills,
            session_store=runtime.store,
            usage_service=runtime.usage,
        )
        agent_config = AgentDefinitionsConfigManager(configured_home)
        configured_agents = agent_config.snapshot()
        agent_definitions = AgentDefinitionService(agent_config)
        supervisor = RuntimeSupervisor(runtime)
        agents = AgentService(
            store=runtime.store,
            definitions=agent_definitions,
            scheduler=supervisor,
            events=runtime.event_recorder,
            resources=runtime.resources,
            spawn_policy=configured_agents.runtime.spawn_policy,
            max_depth=configured_agents.runtime.max_depth,
        )
        runtime.bind_extensions(
            catalog=plugin_runtime,
            agent_definitions=agent_definitions,
            agent_controls=agents,
        )
        plugin_runtime.bind_model_generation(
            ModelGenerationService(
                runtime.models,
                runtime.usage or build_local_usage_service(configured_home),
            )
        )
        task_repository = (
            LocalTaskRepository(configured_home)
            if task_backend == "files"
            else SQLiteTaskRepository(configured_home)
        )
        tasks = TaskService(task_repository, runtime, plugin_tasks=plugin_runtime)
        plugin_runtime.bind_task_service(tasks)
        task_scheduler = TaskScheduler(
            tasks,
            TaskExecutionCoordinator(tasks, supervisor),
            refresh_environment=plugin_runtime.synchronize,
            owned_services=plugin_runtime,
        )
        return cls(
            runtime,
            build_local_mcp_runtime(runtime, home=home),
            plugin_runtime=plugin_runtime,
            plugins=plugins,
            supervisor=supervisor,
            agent_definitions=agent_definitions,
            agents=agents,
            tasks=tasks,
            task_scheduler=task_scheduler,
        )

    async def __aenter__(self) -> LocalApplication:
        await self.start()
        return self

    @contextmanager
    def agent_definition_services(self) -> Iterator[LocalAgentDefinitionServices]:
        """Lease the live tool catalog used by AgentDefinition management."""

        if self.agent_definitions is None:
            raise RuntimeError("Agent definition management is unavailable")
        if self.plugin_runtime is None:
            yield LocalAgentDefinitionServices(
                definitions=self.agent_definitions,
                tools=self.runtime.tools,
            )
            return
        with self.plugin_runtime.snapshot() as catalog:
            yield LocalAgentDefinitionServices(
                definitions=self.agent_definitions,
                tools=catalog.tools,
            )

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("local application is closed")
        if self._started:
            return
        try:
            await self.mcp.start()
            if self.plugin_runtime is not None:
                await self.plugin_runtime.start()
            supervisor_start = await self.supervisor.start()
            self._runtime_startup_diagnostics = supervisor_start.diagnostics
            if self.agents is not None:
                self._agent_startup_diagnostics = await self.agents.start()
            if self.task_scheduler is not None:
                await self.task_scheduler.start()
        except BaseException:
            # Startup failure uses the same ownership order and cancellation-safe
            # cleanup as ordinary shutdown; preserve the original startup error.
            with suppress(Exception):
                await self.close()
            raise
        self._startup_report = self._collect_startup_report()
        self._started = True

    def _collect_startup_report(self) -> ApplicationStartupReport:
        diagnostics = list(self.plugins.startup_diagnostics() if self.plugins is not None else ())
        diagnostics.extend(self._runtime_startup_diagnostics)
        diagnostics.extend(self._agent_startup_diagnostics)
        if self.task_scheduler is not None:
            diagnostics.extend(self.task_scheduler.diagnostics)
        return ApplicationStartupReport(diagnostics=tuple(diagnostics))

    def mcp_runtime_servers(self) -> tuple[MCPRuntimeServerView, ...]:
        return tuple(_mcp_runtime_server_view(record) for record in self.mcp.records)

    async def start_mcp_server(self, server_id: str) -> MCPRuntimeServerView:
        return _mcp_runtime_server_view(await self.mcp.start_server(server_id))

    async def stop_mcp_server(self, server_id: str) -> MCPRuntimeServerView:
        return _mcp_runtime_server_view(await self.mcp.stop_server(server_id))

    async def restart_mcp_server(self, server_id: str) -> MCPRuntimeServerView:
        return _mcp_runtime_server_view(await self.mcp.restart_server(server_id))

    async def refresh_mcp_server_tools(self, server_id: str) -> MCPRuntimeServerView:
        return _mcp_runtime_server_view(await self.mcp.refresh_server_tools(server_id))

    def apply_mcp_configuration(self, config: MCPServerConfig) -> MCPRuntimeServerView:
        """Reconcile durable MCP configuration with the current process runtime."""

        return _mcp_runtime_server_view(self.mcp.apply_config(config))

    async def reconcile_mcp_configuration(
        self,
        config: MCPServerConfig,
    ) -> MCPRuntimeServerView:
        """Apply configuration that may change the live connection lifecycle."""

        return _mcp_runtime_server_view(await self.mcp.reconcile_config(config))

    async def refresh_skills(self, skills: tuple[SkillMetadata, ...]) -> None:
        """Publish a new Host Skill baseline for subsequent Runs."""

        if self.plugin_runtime is None:
            self.runtime.skills = tuple(skill.model_copy(deep=True) for skill in skills)
            return
        await self.plugin_runtime.replace_base_skills(skills)

    def available_skills(self) -> tuple[SkillMetadata, ...]:
        """Return the skills published by the current live extension Catalog."""

        return self.runtime.available_skills()

    async def close(self) -> None:
        self._closed = True
        if self._close_task is not None and self._close_task.done():
            if self._close_task.cancelled() or self._close_task.exception() is not None:
                self._close_task = None
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await settle_owned_tasks(self._close_task)
        self._close_task.result()

    async def _close(self) -> None:
        errors: list[Exception] = []
        if self.provider_auth is not None:
            try:
                await self.provider_auth.close()
            except Exception as exc:
                errors.append(exc)
        if self.task_scheduler is not None:
            try:
                await self.task_scheduler.stop()
            except Exception as exc:
                errors.append(exc)
        if self.agents is not None:
            try:
                await self.agents.close()
            except Exception as exc:  # noqa: BLE001 - close every owned resource.
                errors.append(exc)
        try:
            # Runtime is the consumer of plugin generation snapshots and MCP
            # tools.  Close it first so every run lease is released before its
            # extension owners are drained.
            await self.supervisor.shutdown()
        except Exception as exc:  # noqa: BLE001 - close every owned resource.
            errors.append(exc)
        if self.task_scheduler is not None:
            try:
                await self.task_scheduler.close()
            except Exception as exc:
                errors.append(exc)
        if self.plugin_runtime is not None:
            try:
                await self.plugin_runtime.close()
            except Exception as exc:  # noqa: BLE001 - close every owned resource.
                errors.append(exc)
        try:
            await self.mcp.close()
        except Exception as exc:  # noqa: BLE001 - close every owned resource.
            errors.append(exc)
        if errors:
            raise ExceptionGroup("local application shutdown failed", errors)


@asynccontextmanager
async def open_local_provider_auth(
    home: str | Path | None = None,
    *,
    drivers: tuple[AuthDriver, ...] = (),
) -> AsyncGenerator[ProviderAuthService]:
    """Own authentication alone, using the same built-in drivers as Runtime."""
    root = resolve_local_home(home)
    credentials = CredentialService(FileCredentialStore(root))
    try:
        service = ProviderAuthService(
            ProviderConfigManager(root), credentials, _local_auth_drivers(drivers)
        )
        try:
            yield service
        finally:
            await service.close()
    finally:
        # A failed authorization cleanup must not skip credential writes/locks.
        await credentials.close()


@asynccontextmanager
async def open_local_plugins(
    home: str | Path | None = None,
    *,
    session_backend: SessionBackend | None = None,
) -> AsyncGenerator[PluginService]:
    root = resolve_local_home(home)
    session_services = build_local_session_services(root, session_backend=session_backend)
    usage = build_local_usage_service(root)
    runtime, service = _build_local_plugin_runtime(
        root,
        tools=ToolRegistry(),
        observers=ObserverRegistry(),
        context_providers=ContextProviderRegistry(),
        compaction=CompactionStrategyRegistry(),
        continuations=RunContinuationRegistry(),
        prompt_policies=PromptPolicyRegistry(),
        turn_policies=TurnPolicyRegistry(),
        tool_policies=ToolPolicyRegistry(),
        session_store=session_services._store,
        usage_service=usage,
    )
    credentials = CredentialService(FileCredentialStore(root))

    models = ConfiguredModelCatalog(
        ProviderConfigManager(root),
        adapter_builder=_authenticated_adapter_builder(
            credentials,
            _local_auth_drivers(()),
            _BUILTIN_SERVICE_POLICIES,
            provider_binder=runtime.bind_provider,
        ),
    )
    runtime.bind_model_generation(ModelGenerationService(models, usage))
    try:
        await runtime.start()
        yield service
    finally:
        await runtime.close()
        try:
            await models.close()
        finally:
            await credentials.close()


@asynccontextmanager
async def open_local_agent_definitions(
    home: str | Path | None = None,
    *,
    session_backend: SessionBackend | None = None,
) -> AsyncGenerator[LocalAgentDefinitionServices]:
    """Open definition CRUD without requiring a configured model provider."""

    root = resolve_local_home(home)
    session_services = build_local_session_services(root, session_backend=session_backend)
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    plugin_runtime, _ = _build_local_plugin_runtime(
        root,
        tools=tools,
        observers=ObserverRegistry(),
        context_providers=ContextProviderRegistry(),
        compaction=CompactionStrategyRegistry(),
        continuations=RunContinuationRegistry(),
        prompt_policies=PromptPolicyRegistry(),
        turn_policies=TurnPolicyRegistry(),
        tool_policies=ToolPolicyRegistry(),
        session_store=session_services._store,
        usage_service=build_local_usage_service(root),
    )
    mcp = MCPRuntimeResource.from_config(root, tools)
    try:
        await mcp.start()
        await plugin_runtime.start()
        with plugin_runtime.snapshot() as catalog:
            yield LocalAgentDefinitionServices(
                definitions=AgentDefinitionService(AgentDefinitionsConfigManager(root)),
                tools=catalog.tools,
            )
    finally:
        errors: list[Exception] = []
        for close in (plugin_runtime.close, mcp.close):
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 - close every owned resource.
                errors.append(exc)
        if errors:
            raise ExceptionGroup("local agent definition services shutdown failed", errors)


def build_local_runtime(
    home: str | Path | None = None,
    *,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    event_sink: RuntimeEventSink | None = None,
    session_backend: SessionBackend | None = None,
    auth_drivers: tuple[AuthDriver, ...] = (),
    service_policies: tuple[ProviderServicePolicy, ...] = (),
) -> RuntimeService:
    root = resolve_local_home(home)
    session_services = build_local_session_services(root, session_backend=session_backend)
    store = session_services._store
    usage = build_local_usage_service(root)
    session_data_dir = store.sessions_dir if isinstance(store, LocalSessionStore) else None

    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    configuration = RuntimeConfigurationResolver(root)
    resolved = configuration.snapshot(
        provider_options=provider_options,
        system_prompt=system_prompt,
    )
    credentials = CredentialService(FileCredentialStore(root))
    auth_drivers = _local_auth_drivers(auth_drivers)
    service_policies = (*_BUILTIN_SERVICE_POLICIES, *service_policies)
    auth = ProviderAuthService(ProviderConfigManager(root), credentials, auth_drivers)

    async def bind_provider(protocol: str) -> ProviderBinding:
        return await runtime.bind_provider(protocol)

    adapter_builder = _authenticated_adapter_builder(
        credentials, auth_drivers, service_policies, provider_binder=bind_provider
    )
    adapter = adapter_builder(resolved.provider_options)
    model_catalog = configuration.model_catalog(
        adapter,
        adapter_builder=adapter_builder,
        default_overrides=provider_options,
        default_options=resolved.provider_options,
    )
    context_builder = ContextBuilder()
    compaction_adapter, compaction_adapter_error = _configured_compaction_adapter(
        configuration,
        resolved.compaction,
        adapter_builder=lambda options: model_catalog.adapter_for(
            f"{options.provider_id}/{options.endpoint_id}"
        ),
    )
    runtime = RuntimeService(
        store=store,
        adapter=adapter,
        model_catalog=model_catalog,
        tools=registry,
        context_builder=context_builder,
        system_prompt=resolved.system_prompt,
        additional_system_prompt=resolved.additional_system_prompt,
        prompt_context_resolver=partial(configuration.prompt_context, system_prompt=system_prompt),
        event_sink=event_sink,
        permission_overrides=resolved.permission_overrides,
        plugin_permission_overrides=resolved.plugin_permission_overrides,
        tool_execution_config=resolved.tool_execution,
        run_budget=resolved.run_budget,
        compaction_config=resolved.compaction,
        compaction_adapter=compaction_adapter,
        compaction_adapter_error=compaction_adapter_error,
        compaction_adapter_resolver=(
            lambda: model_catalog.adapter_for(
                configuration.default_model_route()
                if resolved.compaction.model.use_default_model
                else (
                    f"{resolved.compaction.model.provider_id}/"
                    f"{resolved.compaction.model.endpoint_id}"
                )
            )
        )
        if compaction_adapter is not None
        else None,
        queued_input_config=resolved.queued_inputs,
        skills=resolved.skills,
        resources=session_services.resources,
        usage_service=usage,
        default_workspace=Path.cwd(),
        environment=RuntimeEnvironment.local(root, session_data_dir),
        provider_auth=auth,
        credential_service=credentials,
    )
    return runtime


def build_local_services(
    home: str | Path | None = None,
    *,
    session_backend: SessionBackend | None = None,
) -> LocalServices:
    """Build complete local query/command facets without a model Runtime."""

    root = resolve_local_home(home)
    session_services = build_local_session_services(root, session_backend=session_backend)
    return LocalServices(
        sessions=session_services.sessions,
        resources=session_services.resources,
        usage=build_local_usage_service(root),
        configuration=ConfigurationFacade(root),
        plugins=build_local_plugin_service(root, session_store=session_services._store),
    )


def build_local_session_services(
    home: str | Path | None = None,
    *,
    session_backend: SessionBackend | None = None,
) -> LocalSessionServices:
    """Build Session and Resource facets that share one Session repository."""

    root = resolve_local_home(home)
    selected_backend = resolve_session_backend(session_backend)
    store: SessionStore
    if selected_backend == "files":
        store = LocalSessionStore(root)
    else:
        store = SQLiteSessionStore(root / "sessions.sqlite3")
    store.initialize()

    resource_store = LocalResourceStore(root)
    resource_store.initialize()
    resources = ResourceService(store=resource_store, sessions=store)
    return LocalSessionServices(
        sessions=SessionService(store, resources=resources),
        resources=resources,
        _store=store,
    )


def build_local_usage_service(home: str | Path | None = None) -> UsageService:
    """Build the independent local provider-usage facet."""

    root = resolve_local_home(home)
    usage_ledger = LocalUsageLedger(root)
    usage_ledger.initialize()
    return UsageService(usage_ledger)


def build_local_data_transfer_service(
    home: str | Path | None = None,
    *,
    session_backend: SessionBackend | None = None,
    session_store: SessionStore | None = None,
) -> DataTransferService:
    root = resolve_local_home(home)

    def session_transfer() -> SessionTransferService:
        store = session_store
        if store is None:
            store = build_local_session_services(root, session_backend=session_backend)._store
        resources = LocalResourceStore(root)
        resources.initialize()
        return SessionTransferService(store, resources)

    return DataTransferService(root, sessions=session_transfer)


def build_local_plugin_service(
    home: str | Path | None = None,
    *,
    session_store: SessionStore | None = None,
) -> PluginService:
    """Build the static local plugin use-case facade without a runtime owner."""

    store = PluginPackageStore(resolve_local_home(home))
    store.initialize()
    return PluginService(
        store,
        session_store=session_store,
        builtin_plugins=LOCAL_BUILTIN_PLUGINS,
        # Static package operations do not depend on the accounting database.
        # Ledger methods initialize and validate storage when a query needs it.
        usage_service=UsageService(LocalUsageLedger(store.home)),
    )


def build_local_plugin_maintenance_service(
    home: str | Path | None = None,
    *,
    session_backend: SessionBackend | None = None,
) -> PluginService:
    """Inspect recovery references and maintain plugin data or code without activation."""
    root = resolve_local_home(home)
    sessions = build_local_session_services(root, session_backend=session_backend)
    return build_local_plugin_service(root, session_store=sessions._store)


def build_local_mcp_runtime(
    runtime: RuntimeService,
    *,
    home: str | Path | None = None,
) -> MCPRuntimeResource:
    configured_home = runtime.environment.home
    if configured_home is None and home is not None:
        configured_home = resolve_local_home(home)
    if configured_home is None:
        raise OuroError("runtime has no configured deployment home")
    return MCPRuntimeResource.from_config(configured_home, runtime.tools)


def resolve_local_home(home: str | Path | None = None) -> Path:
    return ensure_home_available(home)


def resolve_plugin_credential(reference: str) -> str:
    value = CredentialResolver().resolve(CredentialRef.parse(reference))
    if value is None:
        raise ValueError(f"plugin credential is unavailable: {reference}")
    return value


def _build_local_plugin_runtime(
    home: Path,
    *,
    tools: ToolRegistry,
    observers: ObserverRegistry,
    context_providers: ContextProviderRegistry,
    compaction: CompactionStrategyRegistry,
    continuations: RunContinuationRegistry,
    prompt_policies: PromptPolicyRegistry,
    turn_policies: TurnPolicyRegistry,
    tool_policies: ToolPolicyRegistry,
    skills: tuple[SkillMetadata, ...] = (),
    session_store: SessionStore | None,
    usage_service: UsageService | None,
) -> tuple[PluginRuntimeResource, PluginService]:
    store = PluginPackageStore(home)
    store.initialize()
    commands = PluginCommandRegistry()
    runtime = PluginRuntimeResource(
        store,
        ContributionCoordinator(
            tools=tools,
            observers=observers,
            context_providers=context_providers,
            commands=commands,
            compaction=compaction,
            continuations=continuations,
            prompt_policies=prompt_policies,
            turn_policies=turn_policies,
            tool_policies=tool_policies,
        ),
        credential_resolver=resolve_plugin_credential,
        builtin_plugins=LOCAL_BUILTIN_PLUGINS,
        base_skills=skills,
        session_repository=session_store,
    )
    service = PluginService(
        store,
        runtime=runtime,
        session_store=session_store,
        builtin_plugins=LOCAL_BUILTIN_PLUGINS,
        usage_service=usage_service,
    )
    return runtime, service


def _configured_compaction_adapter(
    configuration: RuntimeConfigurationResolver,
    config: CompactionConfig,
    *,
    adapter_builder: AdapterBuilder,
) -> tuple[RuntimeAdapter | None, str | None]:
    if not config.model.has_configured_route:
        return None, None
    try:
        options = configuration.compaction_provider_options(config)
        assert options is not None
        return adapter_builder(options), None
    except Exception as exc:  # noqa: BLE001 - optional route degrades to the runtime route.
        return (
            None,
            f"configured endpoint unavailable: {type(exc).__name__}: {exc}",
        )


def _authenticated_adapter_builder(
    credentials: CredentialService,
    drivers: tuple[AuthDriver, ...],
    policies: tuple[ProviderServicePolicy, ...],
    *,
    provider_binder: ProviderBinder | None = None,
) -> AdapterBuilder:
    driver_by_id = {driver.descriptor.driver_id: driver for driver in drivers}
    policy_by_id = {policy.policy_id: policy for policy in policies}
    if len(driver_by_id) != len(drivers) or len(policy_by_id) != len(policies):
        raise ConfigError("authentication and service registrations must have unique ids")
    for provider_type in SUPPORTED_PROVIDER_TYPES:
        spec = provider_type_spec(provider_type)
        for connection in spec.connections:
            if connection.auth_driver != "static" and connection.auth_driver not in driver_by_id:
                raise ConfigError(
                    f"authentication driver is not registered: {connection.auth_driver}"
                )
            if (
                connection.service_policy is not None
                and connection.service_policy not in policy_by_id
            ):
                raise ConfigError(
                    f"provider service policy is not registered: {connection.service_policy}"
                )

    def build(options: RuntimeProviderOptions) -> RuntimeAdapter:
        policy = policy_by_id.get(options.service_policy or "")
        if options.service_policy is not None and policy is None:
            raise ConfigError("provider service registration is unavailable")
        if options.auth_driver in (None, "static"):
            return (
                build_runtime_adapter(options, request_policy=policy.requests)
                if policy is not None
                else build_runtime_adapter(
                    options,
                    plugin_provider=partial(build_plugin_provider, bind=provider_binder)
                    if provider_binder is not None
                    else None,
                )
            )
        driver = driver_by_id.get(options.auth_driver)
        if driver is None or policy is None:
            raise ConfigError("managed authentication registration is unavailable")
        ref = options.credential_ref
        if ref is None or ref.type != "managed" or ref.name is None:
            raise ConfigError("managed authentication requires a credential reference")
        authenticator = ManagedRequestAuthenticator(
            credentials, ref.name, driver, service_id=policy.policy_id, targets=policy.targets
        )
        return build_runtime_adapter(
            options, authenticator=authenticator, request_policy=policy.requests
        )

    return build


def _mcp_runtime_server_view(record: MCPServerRecord) -> MCPRuntimeServerView:
    return MCPRuntimeServerView(
        server_id=record.config.server_id,
        name=record.config.name,
        transport=record.config.transport,
        status=record.status,
        tool_count=len(record.tool_names),
        error=record.error,
    )
