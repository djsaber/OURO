from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.agents import AgentService
from ouro.application.configuration import ConfigurationFacade
from ouro.application.dto import ApplicationStartupReport, Diagnostic
from ouro.application.events import RuntimeEventSink
from ouro.application.plugins import PluginService
from ouro.application.resources import ResourceService
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_configuration import (
    RuntimeConfigurationResolver,
    RuntimeProviderOptions,
)
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.sessions import SessionService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.builtin_plugins import LOCAL_BUILTIN_PLUGINS
from ouro.config import CompactionConfig, CredentialRef, CredentialResolver
from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.context import ContextBuilder
from ouro.core.context_providers import ContextProviderRegistry
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.errors import OuroError
from ouro.core.hooks import HookRegistry
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.turn_policies import TurnPolicyRegistry
from ouro.deployment import SessionBackend, resolve_session_backend
from ouro.mcp import MCPRuntimeResource
from ouro.plugins.contributions import ContributionCoordinator
from ouro.plugins.registry import PluginCommandRegistry
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.providers import build_runtime_adapter
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
    ) -> None:
        self.runtime = runtime
        self.mcp = mcp
        self.plugin_runtime = plugin_runtime
        self.plugins = plugins
        self.supervisor = supervisor or RuntimeSupervisor(runtime)
        self.agent_definitions = agent_definitions
        self.agents = agents
        self._startup_report = ApplicationStartupReport()
        self._runtime_startup_diagnostics: tuple[Diagnostic, ...] = ()
        self._agent_startup_diagnostics: tuple[Diagnostic, ...] = ()
        self._started = False
        self._closed = False

    @property
    def startup_report(self) -> ApplicationStartupReport:
        return self._startup_report

    @classmethod
    def build(
        cls,
        home: str | Path | None = None,
        *,
        provider_options: RuntimeProviderOptions | None = None,
        system_prompt: str | None = None,
        event_sink: RuntimeEventSink | None = None,
        session_backend: SessionBackend | None = None,
    ) -> LocalApplication:
        runtime = build_local_runtime(
            home,
            provider_options=provider_options,
            system_prompt=system_prompt,
            event_sink=event_sink,
            session_backend=session_backend,
        )
        return cls.from_runtime(runtime)

    @classmethod
    def from_runtime(
        cls,
        runtime: RuntimeService,
        *,
        home: str | Path | None = None,
    ) -> LocalApplication:
        configured_home = runtime.environment.home
        if configured_home is None and home is not None:
            configured_home = resolve_local_home(home)
        if configured_home is None:
            raise OuroError("runtime has no configured deployment home")
        plugin_runtime, plugins = _build_local_plugin_runtime(
            configured_home,
            tools=runtime.tools,
            hooks=runtime.hooks,
            context_providers=runtime.context_providers,
            compaction=runtime.compaction_strategies,
            continuations=runtime.continuations,
            turn_policies=runtime.turn_policies,
            tool_policies=runtime.tool_policies,
            skills=runtime.skills,
            session_store=runtime.store,
        )
        runtime.attach_extension_catalog(plugin_runtime)
        agent_config = AgentDefinitionsConfigManager(configured_home)
        configured_agents = agent_config.snapshot()
        agent_definitions = AgentDefinitionService(agent_config)
        runtime.attach_agent_definitions(agent_definitions)
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
        runtime.attach_agent_controls(agents)
        return cls(
            runtime,
            build_local_mcp_runtime(runtime, home=home),
            plugin_runtime=plugin_runtime,
            plugins=plugins,
            supervisor=supervisor,
            agent_definitions=agent_definitions,
            agents=agents,
        )

    async def __aenter__(self) -> LocalApplication:
        await self.start()
        return self

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
            self.runtime.reconcile_interrupted_session_controls()
            await self.runtime.reconcile_pending_run_continuations()
            supervisor_start = await self.supervisor.start()
            self._runtime_startup_diagnostics = supervisor_start.diagnostics
            if self.agents is not None:
                self._agent_startup_diagnostics = await self.agents.start()
        except BaseException:
            with suppress(Exception):
                await self.mcp.close()
            if self.plugin_runtime is not None:
                with suppress(Exception):
                    await self.plugin_runtime.close()
            with suppress(Exception):
                await self.supervisor.shutdown()
            self._closed = True
            raise
        self._startup_report = self._collect_startup_report()
        self._started = True

    def _collect_startup_report(self) -> ApplicationStartupReport:
        diagnostics = list(self.plugins.startup_diagnostics() if self.plugins is not None else ())
        diagnostics.extend(self._runtime_startup_diagnostics)
        diagnostics.extend(self._agent_startup_diagnostics)
        diagnostics.extend(
            Diagnostic(
                code="mcp_startup_failed",
                severity="error",
                component="mcp",
                message=(
                    f"MCP server {record.config.server_id} failed to start: "
                    f"{record.error or 'unknown error'}"
                ),
                server_id=record.config.server_id,
                recoverable=True,
                suggested_action="Check the MCP server configuration and retry.",
            )
            for record in self.mcp.startup_failures
        )
        return ApplicationStartupReport(diagnostics=tuple(diagnostics))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
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
async def open_local_plugins(
    home: str | Path | None = None,
    *,
    session_backend: SessionBackend | None = None,
) -> AsyncIterator[PluginService]:
    root = resolve_local_home(home)
    session_services = build_local_session_services(root, session_backend=session_backend)
    runtime, service = _build_local_plugin_runtime(
        root,
        tools=ToolRegistry(),
        hooks=HookRegistry(),
        context_providers=ContextProviderRegistry(),
        compaction=CompactionStrategyRegistry(),
        continuations=RunContinuationRegistry(),
        turn_policies=TurnPolicyRegistry(),
        tool_policies=ToolPolicyRegistry(),
        session_store=session_services._store,
    )
    await runtime.start()
    try:
        yield service
    finally:
        await runtime.close()


@asynccontextmanager
async def open_local_agent_definitions(
    home: str | Path | None = None,
    *,
    session_backend: SessionBackend | None = None,
) -> AsyncIterator[LocalAgentDefinitionServices]:
    """Open definition CRUD without requiring a configured model provider."""

    root = resolve_local_home(home)
    session_services = build_local_session_services(root, session_backend=session_backend)
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    plugin_runtime, _ = _build_local_plugin_runtime(
        root,
        tools=tools,
        hooks=HookRegistry(),
        context_providers=ContextProviderRegistry(),
        compaction=CompactionStrategyRegistry(),
        continuations=RunContinuationRegistry(),
        turn_policies=TurnPolicyRegistry(),
        tool_policies=ToolPolicyRegistry(),
        session_store=session_services._store,
    )
    mcp = MCPRuntimeResource.from_config(root, tools)
    try:
        await mcp.start()
        await plugin_runtime.start()
        yield LocalAgentDefinitionServices(
            definitions=AgentDefinitionService(AgentDefinitionsConfigManager(root)),
            tools=tools,
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
    adapter = build_runtime_adapter(resolved.provider_options)
    model_catalog = configuration.model_catalog(
        adapter,
        adapter_builder=build_runtime_adapter,
    )
    context_builder = ContextBuilder()
    compaction_adapter, compaction_adapter_error = _configured_compaction_adapter(
        configuration,
        resolved.compaction,
    )
    return RuntimeService(
        store=store,
        adapter=adapter,
        model_catalog=model_catalog,
        tools=registry,
        context_builder=context_builder,
        system_prompt=resolved.system_prompt,
        event_sink=event_sink,
        permission_overrides=resolved.permission_overrides,
        tool_execution_config=resolved.tool_execution,
        run_budget=resolved.run_budget,
        compaction_config=resolved.compaction,
        compaction_adapter=compaction_adapter,
        compaction_adapter_error=compaction_adapter_error,
        queued_input_config=resolved.queued_inputs,
        skills=resolved.skills,
        resources=session_services.resources,
        usage_service=usage,
        default_workspace=Path.cwd(),
        environment=RuntimeEnvironment.local(root, session_data_dir),
        owned_auxiliary_adapters=((compaction_adapter,) if compaction_adapter is not None else ()),
    )


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
    )


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
    root = home if home is not None else os.environ.get("OURO_HOME")
    return (Path(root) if root is not None else Path.home() / ".ouro").expanduser().resolve()


def resolve_plugin_credential(reference: str) -> str:
    value = CredentialResolver().resolve(CredentialRef.parse(reference))
    if value is None:
        raise ValueError(f"plugin credential is unavailable: {reference}")
    return value


def _build_local_plugin_runtime(
    home: Path,
    *,
    tools: ToolRegistry,
    hooks: HookRegistry,
    context_providers: ContextProviderRegistry,
    compaction: CompactionStrategyRegistry,
    continuations: RunContinuationRegistry,
    turn_policies: TurnPolicyRegistry,
    tool_policies: ToolPolicyRegistry,
    skills: tuple[SkillMetadata, ...] = (),
    session_store: SessionStore | None,
) -> tuple[PluginRuntimeResource, PluginService]:
    store = PluginPackageStore(home)
    store.initialize()
    commands = PluginCommandRegistry()
    runtime = PluginRuntimeResource(
        store,
        ContributionCoordinator(
            tools=tools,
            hooks=hooks,
            context_providers=context_providers,
            commands=commands,
            compaction=compaction,
            continuations=continuations,
            turn_policies=turn_policies,
            tool_policies=tool_policies,
        ),
        credential_resolver=resolve_plugin_credential,
        builtin_plugins=LOCAL_BUILTIN_PLUGINS,
        base_skills=skills,
    )
    service = PluginService(
        store,
        runtime=runtime,
        session_store=session_store,
        builtin_plugins=LOCAL_BUILTIN_PLUGINS,
    )
    return runtime, service


def _configured_compaction_adapter(
    configuration: RuntimeConfigurationResolver,
    config: CompactionConfig,
) -> tuple[RuntimeAdapter | None, str | None]:
    if not config.model.has_dedicated_endpoint:
        return None, None
    try:
        options = configuration.compaction_provider_options(config)
        assert options is not None
        return build_runtime_adapter(options), None
    except Exception as exc:  # noqa: BLE001 - optional route degrades to the runtime route.
        return (
            None,
            f"configured endpoint unavailable: {type(exc).__name__}: {exc}",
        )
