from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Protocol

from ouro.core.base import new_id
from ouro.core.compaction import (
    CompactionArtifact,
    CompactionRequest,
    CompactionStrategy,
    CompactionStrategyRegistry,
)
from ouro.core.context_providers import (
    ContextProviderCall,
    ContextProviderRegistration,
    ContextProviderRegistry,
    ContextProviderResult,
)
from ouro.core.continuations import (
    ContinuationDecision,
    ContinuationPolicyRegistration,
    RunContinuationRegistry,
)
from ouro.core.observers import Observation, ObserverRegistration, ObserverRegistry
from ouro.core.prompt_policies import (
    PromptPolicyCall,
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.state import RunOutcome
from ouro.core.tool_policies import (
    ToolPolicyCall,
    ToolPolicyDecision,
    ToolPolicyRegistration,
    ToolPolicyRegistry,
)
from ouro.core.turn_policies import (
    TurnCompletionCandidate,
    TurnPolicyDecision,
    TurnPolicyRegistration,
    TurnPolicyRegistry,
)
from ouro.plugins.adapters import CatalogProjectionTargets, project_prepared_contributions
from ouro.plugins.contributions import PreparedContributionSet
from ouro.plugins.models import PluginClass, PluginGenerationRecord
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandRegistry,
    PluginCommandRequest,
    PluginCommandResult,
)
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerRegistry,
)
from ouro.skills import SkillMetadata
from ouro.tools import Tool, ToolExecutionContext, ToolInputRequest, ToolRegistry, ToolResult


class PluginGenerationUnavailableError(RuntimeError):
    """Raised when a callback targets a generation whose invocation gate is closed."""


class GenerationGate:
    """Process-local admission gate shared by every callback in one generation."""

    def __init__(self, generation_id: str, *, initially_open: bool = True) -> None:
        self.generation_id = generation_id
        self._open = initially_open
        self._in_flight = 0
        self._lock = threading.Lock()
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def close(self) -> None:
        with self._lock:
            self._open = False
            if self._in_flight == 0:
                self._drained.set()

    def open(self) -> None:
        with self._lock:
            if self._open:
                return
            self._open = True

    async def wait_drained(self) -> None:
        await self._drained.wait()

    @asynccontextmanager
    async def invocation(self):
        with self._lock:
            if not self._open:
                raise PluginGenerationUnavailableError(
                    f"plugin generation is unavailable: {self.generation_id}"
                )
            self._in_flight += 1
            self._drained.clear()
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
                if self._in_flight == 0:
                    self._drained.set()


class GenerationLease(Protocol):
    @property
    def record(self) -> PluginGenerationRecord: ...

    def close(self) -> None: ...


class CatalogGeneration(Protocol):
    record: PluginGenerationRecord
    contributions: PreparedContributionSet
    gate: GenerationGate

    def acquire(self) -> GenerationLease: ...


@dataclass(frozen=True)
class ContributionOwnerRecord:
    canonical_identity: str
    owner_id: str
    plugin_class: PluginClass | None
    plugin_id: str | None
    release_id: str | None
    package_digest: str | None
    builtin_contract_hash: str | None
    generation_id: str | None
    contract_hash: str


@dataclass(frozen=True)
class ExtensionCatalogBase:
    tools: tuple[dict[str, Tool], dict[str, str]]
    tool_registry_revision: int
    observers: list[ObserverRegistration]
    context_providers: list[ContextProviderRegistration]
    commands: dict[str, PluginCommand]
    compaction: tuple[dict[str, CompactionStrategy], dict[str, str]]
    continuations: list[ContinuationPolicyRegistration]
    prompt_policies: list[PromptPolicyRegistration]
    turn_policies: list[TurnPolicyRegistration]
    tool_policies: list[ToolPolicyRegistration]
    skills: tuple[SkillMetadata, ...]
    session_controllers: dict[str, PluginSessionController]

    @classmethod
    def capture(
        cls,
        *,
        tools: ToolRegistry,
        observers: ObserverRegistry,
        context_providers: ContextProviderRegistry | None = None,
        commands: PluginCommandRegistry | None,
        compaction: CompactionStrategyRegistry | None,
        continuations: RunContinuationRegistry | None,
        prompt_policies: PromptPolicyRegistry | None = None,
        turn_policies: TurnPolicyRegistry | None = None,
        tool_policies: ToolPolicyRegistry | None,
        skills: Sequence[SkillMetadata] = (),
        session_controllers: PluginSessionControllerRegistry | None = None,
    ) -> ExtensionCatalogBase:
        captured_skills = tuple(skill.model_copy(deep=True) for skill in skills)
        skill_names = [skill.name for skill in captured_skills]
        if len(skill_names) != len(set(skill_names)):
            raise ValueError("Host baseline contains duplicate skill names")
        return cls(
            tools=tools.snapshot(),
            tool_registry_revision=tools.revision,
            observers=observers.snapshot(),
            context_providers=(context_providers or ContextProviderRegistry()).snapshot(),
            commands=commands.snapshot() if commands is not None else {},
            compaction=(compaction or CompactionStrategyRegistry()).snapshot(),
            continuations=(continuations or RunContinuationRegistry()).snapshot(),
            prompt_policies=(prompt_policies or PromptPolicyRegistry()).snapshot(),
            turn_policies=(turn_policies or TurnPolicyRegistry()).snapshot(),
            tool_policies=(tool_policies or ToolPolicyRegistry()).snapshot(),
            skills=captured_skills,
            session_controllers=(
                session_controllers or PluginSessionControllerRegistry()
            ).snapshot(),
        )


class ExtensionCatalog:
    """Immutable contribution directory built without mutating its Host baseline."""

    def __init__(
        self,
        *,
        generations: Sequence[CatalogGeneration],
        tools: ToolRegistry,
        observers: ObserverRegistry,
        context_providers: ContextProviderRegistry,
        commands: PluginCommandRegistry,
        compaction: CompactionStrategyRegistry,
        continuations: RunContinuationRegistry,
        prompt_policies: PromptPolicyRegistry,
        turn_policies: TurnPolicyRegistry,
        tool_policies: ToolPolicyRegistry,
        skills: Sequence[SkillMetadata],
        session_controllers: PluginSessionControllerRegistry,
        owners: Mapping[str, ContributionOwnerRecord],
    ) -> None:
        self.catalog_id = new_id("cat")
        self._generation_sources = tuple(generations)
        self.generations = tuple(
            source.record.model_copy(deep=True) for source in self._generation_sources
        )
        self.tools = tools
        self.observers = observers
        self.context_providers = context_providers
        self.commands = commands
        self.compaction = compaction
        self.continuations = continuations
        self.prompt_policies = prompt_policies
        self.turn_policies = turn_policies
        self.tool_policies = tool_policies
        self.session_controllers = session_controllers
        self._skills = tuple(skill.model_copy(deep=True) for skill in skills)
        self.owners = MappingProxyType(dict(owners))
        self._lease_count = 0
        self._lock = threading.Lock()

    @property
    def lease_count(self) -> int:
        with self._lock:
            return self._lease_count

    @property
    def skills(self) -> tuple[SkillMetadata, ...]:
        return tuple(skill.model_copy(deep=True) for skill in self._skills)

    def acquire(
        self,
        *,
        on_close: Callable[[ExtensionCatalog], None] | None = None,
    ) -> ExtensionCatalogLease:
        leases: list[GenerationLease] = []
        try:
            for source in self._generation_sources:
                leases.append(source.acquire())
        except Exception:
            for lease in reversed(leases):
                lease.close()
            raise
        with self._lock:
            self._lease_count += 1
        return ExtensionCatalogLease(self, tuple(leases), on_close=on_close)

    def rebase(self, base: ExtensionCatalogBase) -> ExtensionCatalog:
        """Project the same plugin generations over a new Host baseline."""

        return build_extension_catalog(base, self._generation_sources)

    def contract_hash(self, canonical_identity: str) -> str | None:
        owner = self.owners.get(canonical_identity)
        return owner.contract_hash if owner is not None else None

    def matches(self, expected_generations: Sequence[Mapping[str, object]]) -> bool:
        expected = {str(item.get("plugin_id", "")): item for item in expected_generations}
        if "" in expected or len(expected) != len(expected_generations):
            return False
        actual = {generation.plugin_id: generation for generation in self.generations}
        if set(expected) != set(actual):
            return False
        return all(
            generation_matches_execution_contract(generation, expected[plugin_id])
            for plugin_id, generation in actual.items()
        )

    def _release(self) -> None:
        with self._lock:
            if self._lease_count:
                self._lease_count -= 1


class ExtensionCatalogLease:
    """One consumer's lease on an immutable Extension Catalog."""

    def __init__(
        self,
        catalog: ExtensionCatalog,
        generation_leases: tuple[GenerationLease, ...],
        *,
        on_close: Callable[[ExtensionCatalog], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self._generation_leases = generation_leases
        self._on_close = on_close
        self._closed = False

    @property
    def catalog_id(self) -> str:
        return self.catalog.catalog_id

    @property
    def generations(self) -> tuple[PluginGenerationRecord, ...]:
        return self.catalog.generations

    @property
    def tools(self) -> ToolRegistry:
        return self.catalog.tools

    @property
    def observers(self) -> ObserverRegistry:
        return self.catalog.observers

    @property
    def context_providers(self) -> ContextProviderRegistry:
        return self.catalog.context_providers

    @property
    def commands(self) -> PluginCommandRegistry:
        return self.catalog.commands

    @property
    def compaction(self) -> CompactionStrategyRegistry:
        return self.catalog.compaction

    @property
    def continuations(self) -> RunContinuationRegistry:
        return self.catalog.continuations

    @property
    def prompt_policies(self) -> PromptPolicyRegistry:
        return self.catalog.prompt_policies

    @property
    def turn_policies(self) -> TurnPolicyRegistry:
        return self.catalog.turn_policies

    @property
    def tool_policies(self) -> ToolPolicyRegistry:
        return self.catalog.tool_policies

    @property
    def skills(self) -> tuple[SkillMetadata, ...]:
        return self.catalog.skills

    @property
    def session_controllers(self) -> PluginSessionControllerRegistry:
        return self.catalog.session_controllers

    def contract_hash(self, canonical_identity: str) -> str | None:
        return self.catalog.contract_hash(canonical_identity)

    def fork(self) -> ExtensionCatalogLease:
        if self._closed:
            raise RuntimeError("extension catalog lease is closed")
        return self.catalog.acquire(on_close=self._on_close)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for lease in reversed(self._generation_leases):
            lease.close()
        self.catalog._release()
        if self._on_close is not None:
            self._on_close(self.catalog)

    def __enter__(self) -> ExtensionCatalogLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def build_extension_catalog(
    base: ExtensionCatalogBase,
    generations: Sequence[CatalogGeneration],
) -> ExtensionCatalog:
    tools = ToolRegistry()
    tools.restore(base.tools)
    observers = ObserverRegistry()
    observers.restore(base.observers)
    context_providers = ContextProviderRegistry()
    context_providers.restore(base.context_providers)
    commands = PluginCommandRegistry()
    commands.restore(base.commands)
    compaction = CompactionStrategyRegistry()
    compaction.restore(base.compaction)
    continuations = RunContinuationRegistry()
    continuations.restore(base.continuations)
    prompt_policies = PromptPolicyRegistry()
    prompt_policies.restore(base.prompt_policies)
    turn_policies = TurnPolicyRegistry()
    turn_policies.restore(base.turn_policies)
    tool_policies = ToolPolicyRegistry()
    tool_policies.restore(base.tool_policies)
    session_controllers = PluginSessionControllerRegistry()
    session_controllers.restore(base.session_controllers)
    skills = {skill.name: skill.model_copy(deep=True) for skill in base.skills}
    owners = _base_owner_records(base)
    targets = CatalogProjectionTargets(
        tools=tools,
        observers=observers,
        context_providers=context_providers,
        commands=commands,
        compaction=compaction,
        continuations=continuations,
        prompt_policies=prompt_policies,
        turn_policies=turn_policies,
        tool_policies=tool_policies,
        skills=skills,
        session_controllers=session_controllers,
    )

    for source in sorted(generations, key=lambda item: item.record.plugin_id):
        prepared = bind_generation_gate(source.contributions, source.gate)
        owner = prepared.owner.key
        project_prepared_contributions(targets, owner=owner, prepared=prepared)
        for identity, contract_hash in prepared.contract_hashes.items():
            if identity in owners:
                conflict = owners[identity]
                raise ValueError(
                    "catalog contribution identity conflict: "
                    f"{identity} ({conflict.owner_id}, {prepared.owner.key})"
                )
            owners[identity] = ContributionOwnerRecord(
                canonical_identity=identity,
                owner_id=prepared.owner.key,
                plugin_class=source.record.plugin_class,
                plugin_id=source.record.plugin_id,
                release_id=source.record.release_id,
                package_digest=source.record.package_digest,
                builtin_contract_hash=source.record.builtin_contract_hash,
                generation_id=source.record.generation_id,
                contract_hash=contract_hash,
            )
    tools.freeze()
    observers.freeze()
    context_providers.freeze()
    commands.freeze()
    compaction.freeze()
    continuations.freeze()
    prompt_policies.freeze()
    turn_policies.freeze()
    tool_policies.freeze()
    session_controllers.freeze()
    return ExtensionCatalog(
        generations=generations,
        tools=tools,
        observers=observers,
        context_providers=context_providers,
        commands=commands,
        compaction=compaction,
        continuations=continuations,
        prompt_policies=prompt_policies,
        turn_policies=turn_policies,
        tool_policies=tool_policies,
        session_controllers=session_controllers,
        skills=tuple(skills[name] for name in sorted(skills)),
        owners=owners,
    )


def bind_generation_gate(
    prepared: PreparedContributionSet,
    gate: GenerationGate,
) -> PreparedContributionSet:
    return replace(
        prepared,
        tools=tuple(_gated_tool(item, gate) for item in prepared.tools),
        observers=tuple(_gated_observer(item, gate) for item in prepared.observers),
        context_providers=tuple(
            _gated_context_provider(item, gate) for item in prepared.context_providers
        ),
        commands=tuple(_gated_command(item, gate) for item in prepared.commands),
        compaction_strategies=tuple(
            _GatedCompactionStrategy(item, gate) for item in prepared.compaction_strategies
        ),
        continuation_policies=tuple(
            _gated_continuation(item, gate) for item in prepared.continuation_policies
        ),
        prompt_policies=tuple(
            _gated_prompt_policy(item, gate) for item in prepared.prompt_policies
        ),
        turn_policies=tuple(_gated_turn_policy(item, gate) for item in prepared.turn_policies),
        tool_policies=tuple(_gated_tool_policy(item, gate) for item in prepared.tool_policies),
        session_controllers=tuple(
            _gated_session_controller(item, gate) for item in prepared.session_controllers
        ),
    )


def _gated_session_controller(
    controller: PluginSessionController,
    gate: GenerationGate,
) -> PluginSessionController:
    async def activate(call):
        async with gate.invocation():
            return await _invoke_callback(controller.activate_handler, call)

    async def deactivate(call):
        async with gate.invocation():
            return await _invoke_callback(controller.deactivate_handler, call)

    return replace(
        controller,
        activate_handler=activate,
        deactivate_handler=deactivate,
    )


def _gated_tool(tool: Tool, gate: GenerationGate) -> Tool:
    async def invoke(
        invocation,
        *,
        context: ToolExecutionContext | None = None,
        signal=None,
    ) -> ToolResult | ToolInputRequest:
        async with gate.invocation():
            return await tool.execute(invocation, context=context, signal=signal)

    async def resume(
        invocation,
        resolution,
        *,
        request,
        context: ToolExecutionContext | None = None,
        signal=None,
    ):
        async with gate.invocation():
            return await tool.resume(
                invocation,
                resolution,
                request=request,
                context=context,
                signal=signal,
            )

    return Tool(
        definition=tool.definition,
        handler=invoke,
        resume_handler=resume if tool.resume_handler is not None else None,
    )


def _gated_observer(
    registration: ObserverRegistration,
    gate: GenerationGate,
) -> ObserverRegistration:
    async def invoke(observation: Observation) -> None:
        async with gate.invocation():
            result = await _invoke_callback(registration.handler, observation)
            if result is not None:
                raise TypeError("observer handler must return None")

    return replace(registration, handler=invoke)


def _gated_context_provider(
    registration: ContextProviderRegistration,
    gate: GenerationGate,
) -> ContextProviderRegistration:
    async def invoke(call: ContextProviderCall) -> ContextProviderResult:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, call)

    return replace(registration, handler=invoke)


def _gated_command(
    command: PluginCommand,
    gate: GenerationGate,
) -> PluginCommand:
    async def invoke(
        request: PluginCommandRequest,
    ) -> PluginCommandResult | str | Mapping[str, Any]:
        async with gate.invocation():
            return await _invoke_callback(command.handler, request)

    return replace(command, handler=invoke)


@dataclass
class _GatedCompactionStrategy:
    delegate: CompactionStrategy
    gate: GenerationGate
    strategy_id: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        self.strategy_id = self.delegate.strategy_id
        self.version = self.delegate.version

    async def compact(
        self,
        request: CompactionRequest,
        *,
        signal=None,
    ) -> CompactionArtifact:
        async with self.gate.invocation():
            return await self.delegate.compact(request, signal=signal)


def _gated_continuation(
    registration: ContinuationPolicyRegistration,
    gate: GenerationGate,
) -> ContinuationPolicyRegistration:
    async def invoke(outcome: RunOutcome) -> ContinuationDecision:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, outcome)

    return replace(registration, handler=invoke)


def _gated_tool_policy(
    registration: ToolPolicyRegistration,
    gate: GenerationGate,
) -> ToolPolicyRegistration:
    async def invoke(call: ToolPolicyCall) -> ToolPolicyDecision:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, call)

    return replace(registration, handler=invoke)


def _gated_prompt_policy(
    registration: PromptPolicyRegistration,
    gate: GenerationGate,
) -> PromptPolicyRegistration:
    async def invoke(call: PromptPolicyCall) -> PromptPolicyDecision:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, call)

    return replace(registration, handler=invoke)


def _gated_turn_policy(
    registration: TurnPolicyRegistration,
    gate: GenerationGate,
) -> TurnPolicyRegistration:
    async def invoke(candidate: TurnCompletionCandidate) -> TurnPolicyDecision:
        async with gate.invocation():
            return await _invoke_callback(registration.handler, candidate)

    return replace(registration, handler=invoke)


async def _invoke_callback(handler: Callable[[Any], Any], argument: Any) -> Any:
    if inspect.iscoroutinefunction(handler):
        return await handler(argument)
    result = await asyncio.to_thread(handler, argument)
    if inspect.isawaitable(result):
        return await result
    return result


def _base_owner_records(
    base: ExtensionCatalogBase,
) -> dict[str, ContributionOwnerRecord]:
    records: dict[str, ContributionOwnerRecord] = {}
    for name, tool in base.tools[0].items():
        identity = f"ouro.host/tools/{name}"
        records[identity] = _host_owner(
            identity,
            _hash(tool.definition.model_dump(mode="json", exclude={"metadata"})),
        )
    for observer_registration in base.observers:
        identity = (
            "ouro.host/observers/"
            f"{observer_registration.plugin_id}:{observer_registration.observer_id}"
        )
        records[identity] = _host_owner(
            identity,
            _hash(
                {
                    "event": observer_registration.event,
                    "event_types": list(observer_registration.event_types),
                    "source": observer_registration.source,
                    "priority": observer_registration.priority,
                    "enabled": observer_registration.enabled,
                    "timeout_seconds": observer_registration.timeout_seconds,
                }
            ),
        )
    for provider in base.context_providers:
        identity = f"ouro.host/context_providers/{provider.plugin_id}:{provider.provider_id}"
        records[identity] = _host_owner(
            identity,
            _hash(
                {
                    "priority": provider.priority,
                    "timeout_seconds": provider.timeout_seconds,
                    "failure_mode": provider.failure_mode,
                }
            ),
        )
    for identity, command in base.commands.items():
        canonical = f"ouro.host/commands/{identity}"
        records[canonical] = _host_owner(
            canonical,
            _hash({"description": command.description}),
        )
    for strategy_id, strategy in base.compaction[0].items():
        identity = f"ouro.host/compaction_strategies/{strategy_id}"
        records[identity] = _host_owner(identity, _hash({"version": strategy.version}))
    for continuation_registration in base.continuations:
        identity = (
            "ouro.host/continuation_policies/"
            f"{continuation_registration.plugin_id}:{continuation_registration.policy_id}"
        )
        records[identity] = _host_owner(
            identity,
            _hash(
                {
                    "priority": continuation_registration.priority,
                    "timeout_seconds": continuation_registration.timeout_seconds,
                }
            ),
        )
    for tool_policy_registration in base.tool_policies:
        identity = (
            "ouro.host/tool_policies/"
            f"{tool_policy_registration.plugin_id}:{tool_policy_registration.policy_id}"
        )
        records[identity] = _host_owner(
            identity,
            _hash(
                {
                    "priority": tool_policy_registration.priority,
                    "timeout_seconds": tool_policy_registration.timeout_seconds,
                }
            ),
        )
    for prompt_policy_registration in base.prompt_policies:
        identity = (
            "ouro.host/prompt_policies/"
            f"{prompt_policy_registration.plugin_id}:{prompt_policy_registration.policy_id}"
        )
        records[identity] = _host_owner(
            identity,
            _hash(
                {
                    "priority": prompt_policy_registration.priority,
                    "timeout_seconds": prompt_policy_registration.timeout_seconds,
                }
            ),
        )
    for turn_policy_registration in base.turn_policies:
        identity = (
            "ouro.host/turn_policies/"
            f"{turn_policy_registration.plugin_id}:{turn_policy_registration.policy_id}"
        )
        records[identity] = _host_owner(
            identity,
            _hash(
                {
                    "priority": turn_policy_registration.priority,
                    "timeout_seconds": turn_policy_registration.timeout_seconds,
                }
            ),
        )
    for skill in base.skills:
        identity = f"ouro.host/skills/{skill.name}"
        records[identity] = _host_owner(
            identity,
            _hash(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "location": skill.location,
                }
            ),
        )
    return records


def generation_matches_execution_contract(
    generation: PluginGenerationRecord,
    expected: Mapping[str, object],
) -> bool:
    """Match durable facts while leaving process-local generation identity diagnostic-only."""

    return (
        generation.execution_contract_hash is not None
        and generation.execution_contract_hash == expected.get("execution_contract_hash")
        and generation.version == expected.get("version")
        and generation.plugin_class == expected.get("plugin_class", "user")
        and generation.release_id == expected.get("release_id", expected.get("package_digest"))
        and generation.package_digest == expected.get("package_digest")
        and generation.builtin_contract_hash == expected.get("builtin_contract_hash")
        and generation.runtime_kind == expected.get("runtime_kind")
        and generation.configuration_revision_id == expected.get("configuration_revision_id")
        and generation.dependency_lock_fingerprint == expected.get("dependency_lock_fingerprint")
        and generation.plugin_api_version == expected.get("plugin_api_version")
        and generation.worker_protocol_version == expected.get("worker_protocol_version")
        and generation.contract_hashes == expected.get("contract_hashes")
    )


def _host_owner(identity: str, contract_hash: str) -> ContributionOwnerRecord:
    return ContributionOwnerRecord(
        canonical_identity=identity,
        owner_id="ouro.host",
        plugin_class="builtin",
        plugin_id="ouro.host",
        release_id=None,
        package_digest=None,
        builtin_contract_hash=None,
        generation_id=None,
        contract_hash=contract_hash,
    )


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ContributionOwnerRecord",
    "ExtensionCatalog",
    "ExtensionCatalogBase",
    "ExtensionCatalogLease",
    "GenerationGate",
    "PluginGenerationUnavailableError",
    "build_extension_catalog",
    "generation_matches_execution_contract",
]
