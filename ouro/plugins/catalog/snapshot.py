"""Catalog construction, immutable snapshots and leases."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from ouro.core.base import new_id
from ouro.core.compaction import CompactionStrategy, CompactionStrategyRegistry
from ouro.core.context_providers import ContextProviderRegistration, ContextProviderRegistry
from ouro.core.continuations import ContinuationPolicyRegistration, RunContinuationRegistry
from ouro.core.observers import Observation, ObserverRegistration, ObserverRegistry
from ouro.core.prompt_policies import PromptPolicyRegistration, PromptPolicyRegistry
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.tool_policies import ToolPolicyRegistration, ToolPolicyRegistry
from ouro.core.turn_policies import TurnPolicyRegistration, TurnPolicyRegistry
from ouro.plugins.catalog.bindings import bind_generation_gate
from ouro.plugins.catalog.gates import (
    CatalogGeneration,
    CatalogToolGroup,
    GenerationLease,
    acquire_generations,
)
from ouro.plugins.contributions.projection import project_prepared_contributions
from ouro.plugins.contributions.projection_adapters import CatalogProjectionTargets
from ouro.plugins.implementation_contracts import plugin_implementation
from ouro.plugins.models import PluginClass, PluginGenerationRecord
from ouro.plugins.providers import ProviderRegistration, ProviderRegistry
from ouro.plugins.registry import PluginCommand, PluginCommandRegistry
from ouro.plugins.run_drivers import RunDriverRegistration, RunDriverRegistry
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerRegistry,
)
from ouro.skills import SkillMetadata
from ouro.tools import Tool, ToolRegistry


class CatalogContributionError(ValueError):
    """Identify the generation whose contribution prevents catalog publication."""

    def __init__(self, plugin_id: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.plugin_id = plugin_id


@dataclass
class ContributionBinding[T]:
    """Selected contributions and their leases, without retaining a whole catalog."""

    value: T
    _leases: tuple[GenerationLease, ...]

    def close(self) -> None:
        leases, self._leases = self._leases, ()
        for lease in reversed(leases):
            lease.close()

    def __enter__(self) -> ContributionBinding[T]:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


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
        providers: ProviderRegistry,
        run_drivers: RunDriverRegistry,
        continuations: RunContinuationRegistry,
        prompt_policies: PromptPolicyRegistry,
        turn_policies: TurnPolicyRegistry,
        tool_policies: ToolPolicyRegistry,
        skills: Sequence[SkillMetadata],
        session_controllers: PluginSessionControllerRegistry,
        owners: Mapping[str, ContributionOwnerRecord],
        dependencies: Mapping[str, tuple[str, ...]] | None = None,
        tool_groups: Sequence[CatalogToolGroup] = (),
    ) -> None:
        self.catalog_id = new_id("cat")
        self._generation_sources = tuple(generations)
        self._tool_groups = tuple(tool_groups)
        self._generations_by_plugin = {source.record.plugin_id: source for source in generations}
        self._dependencies = dict(dependencies or {})
        self.generations = tuple(
            source.record.model_copy(deep=True) for source in self._generation_sources
        )
        self.tools = tools
        self.observers = observers
        self.context_providers = context_providers
        self.commands = commands
        self.compaction = compaction
        self.providers = providers
        self.run_drivers = run_drivers
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
        leases = acquire_generations((*self._generation_sources, *self._tool_groups))
        with self._lock:
            self._lease_count += 1
        return ExtensionCatalogLease(self, leases, on_close=on_close)

    def rebase(self, base: ExtensionCatalogBase) -> ExtensionCatalog:
        """Project the same plugin generations over a new Host baseline."""

        return build_extension_catalog(
            base,
            self._generation_sources,
            dependencies=self._dependencies,
            tool_groups=self._tool_groups,
        )

    def with_tool_groups(
        self, base: ExtensionCatalogBase, groups: Sequence[CatalogToolGroup]
    ) -> ExtensionCatalog:
        """Rebuild over the same baseline and generations before atomic publication."""
        return build_extension_catalog(
            base,
            self._generation_sources,
            dependencies=self._dependencies,
            tool_groups=groups,
        )

    def bind_provider(self, provider_id: str) -> ContributionBinding[ProviderRegistration]:
        provider = self.providers.get(provider_id)
        return ContributionBinding(provider, self._acquire_plugins({provider.plugin_id}))

    def bind_driver(
        self, driver_id: str, *, expected: RunDriverBinding | None = None
    ) -> ContributionBinding[RunDriverRegistration]:
        driver = self._resolve_driver(driver_id)
        if expected is not None and driver.binding != expected:
            raise ValueError(f"Run driver execution contract is unavailable: {driver_id}")
        return ContributionBinding(driver, self._acquire_plugins({driver.plugin_id}))

    def driver_binding(self, driver_id: str) -> RunDriverBinding:
        """Read implementation identities without acquiring execution resources."""
        return self._resolve_driver(driver_id).binding

    def _resolve_driver(self, driver_id: str) -> RunDriverRegistration:
        driver = self.run_drivers.get(driver_id)
        ordered = self._ordered_plugins({driver.plugin_id})
        implementations = {
            plugin_id: plugin_implementation(self._generations_by_plugin[plugin_id].record)
            for plugin_id in ordered
        }
        return replace(
            driver,
            implementation=implementations[driver.plugin_id],
            dependencies=tuple(
                implementations[plugin_id]
                for plugin_id in sorted(implementations)
                if plugin_id != driver.plugin_id
            ),
        )

    def bind_command(
        self,
        command_id: str,
        *,
        controller_ids: set[str] | None = None,
    ) -> ContributionBinding[tuple[PluginCommandRegistry, PluginSessionControllerRegistry]]:
        command = self.commands.get(command_id)
        commands = PluginCommandRegistry()
        commands.register(command)
        commands.freeze()
        controllers = PluginSessionControllerRegistry()
        selected = tuple(
            item
            for item in self.session_controllers.list()
            if item.plugin_id == command.plugin_id or item.canonical_id in (controller_ids or ())
        )
        controllers.restore({item.canonical_id: item for item in selected})
        controllers.freeze()
        leases = self._acquire_plugins({command.plugin_id, *(item.plugin_id for item in selected)})
        return ContributionBinding((commands, controllers), leases)

    def bind_observers(
        self,
        observation: Observation,
        *,
        active_controller_ids: Callable[[], set[str]],
    ) -> ContributionBinding[ObserverRegistry]:
        registrations = [
            item
            for item in self.observers.scoped(active_controller_ids).matching(observation)
            if (owner := self._generations_by_plugin.get(item.plugin_id)) is None
            or owner.gate.is_open
        ]
        observers = ObserverRegistry()
        observers.restore(registrations)
        observers.freeze()
        leases = self._acquire_plugins({item.plugin_id for item in registrations})
        return ContributionBinding(observers, leases)

    def _ordered_plugins(self, plugin_ids: set[str]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        pending = [(plugin_id, False) for plugin_id in sorted(plugin_ids, reverse=True)]
        while pending:
            plugin_id, expanded = pending.pop()
            if expanded:
                ordered.append(plugin_id)
            elif plugin_id not in seen and plugin_id in self._generations_by_plugin:
                seen.add(plugin_id)
                pending.append((plugin_id, True))
                pending.extend(
                    (dependency_id, False)
                    for dependency_id in reversed(self._dependencies.get(plugin_id, ()))
                )
        return ordered

    def _acquire_plugins(self, plugin_ids: set[str]) -> tuple[GenerationLease, ...]:
        return acquire_generations(
            self._generations_by_plugin[plugin_id]
            for plugin_id in self._ordered_plugins(plugin_ids)
        )

    def contract_hash(self, canonical_identity: str) -> str | None:
        owner = self.owners.get(canonical_identity)
        return owner.contract_hash if owner is not None else None

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
    *,
    dependencies: Mapping[str, tuple[str, ...]] | None = None,
    tool_groups: Sequence[CatalogToolGroup] = (),
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
    providers = ProviderRegistry()
    run_drivers = RunDriverRegistry()
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
        providers=providers,
        run_drivers=run_drivers,
        continuations=continuations,
        prompt_policies=prompt_policies,
        turn_policies=turn_policies,
        tool_policies=tool_policies,
        skills=skills,
        session_controllers=session_controllers,
    )

    sources: list[tuple[str, CatalogGeneration]] = [
        (source.contributions.owner.key, source)
        for source in sorted(generations, key=lambda item: item.record.plugin_id)
    ]
    sources.extend(
        (f"{group.contributions.owner.key}/tool_groups/{group.group_id}", group)
        for group in tool_groups
    )
    for owner, source in sources:
        prepared = bind_generation_gate(source.contributions, source.gate)
        try:
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
                    owner_id=owner,
                    plugin_class=source.record.plugin_class,
                    plugin_id=source.record.plugin_id,
                    release_id=source.record.release_id,
                    package_digest=source.record.package_digest,
                    builtin_contract_hash=source.record.builtin_contract_hash,
                    generation_id=source.record.generation_id,
                    contract_hash=contract_hash,
                )
        except Exception as exc:
            raise CatalogContributionError(source.record.plugin_id, exc) from exc
    tools.freeze()
    observers.freeze()
    context_providers.freeze()
    commands.freeze()
    compaction.freeze()
    providers.freeze()
    run_drivers.freeze()
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
        providers=providers,
        run_drivers=run_drivers,
        continuations=continuations,
        prompt_policies=prompt_policies,
        turn_policies=turn_policies,
        tool_policies=tool_policies,
        session_controllers=session_controllers,
        skills=tuple(skills[name] for name in sorted(skills)),
        owners=owners,
        dependencies=dependencies,
        tool_groups=tool_groups,
    )


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
