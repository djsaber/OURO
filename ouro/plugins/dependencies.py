from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ouro.plugins.models import PluginClass, PluginStateDocument
from ouro.plugins.semver import version_satisfies


class PluginDependencyError(ValueError):
    pass


@dataclass(frozen=True)
class BuiltinPluginContract:
    plugin_id: str
    version: str
    contract_hash: str
    dependencies: tuple[tuple[str, str], ...] = ()
    default_enabled: bool = False


@dataclass(frozen=True)
class LockedPluginDependency:
    plugin_id: str
    version: str
    plugin_class: PluginClass
    package_digest: str | None
    contract_hash: str | None = None
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class PluginLockGraph:
    nodes: tuple[LockedPluginDependency, ...]

    @property
    def activation_order(self) -> tuple[str, ...]:
        return tuple(node.plugin_id for node in self.nodes)

    @property
    def shutdown_order(self) -> tuple[str, ...]:
        return tuple(reversed(self.activation_order))


@dataclass(frozen=True)
class PluginActivationPlan:
    """Best-effort startup plan with failures isolated to each desired root."""

    graph: PluginLockGraph
    failures: tuple[tuple[str, PluginDependencyError], ...] = ()


def plan_desired_plugin_activation(
    state: PluginStateDocument,
    *,
    builtin_contracts: Mapping[str, BuiltinPluginContract] | None = None,
) -> PluginActivationPlan:
    builtin_contracts = builtin_contracts or {}
    roots = sorted(
        {
            *(
                plugin_id
                for plugin_id, record in state.plugins.items()
                if record.activation.desired_enabled
            ),
            *(
                plugin_id
                for plugin_id, contract in builtin_contracts.items()
                if builtin_desired_enabled(state, contract)
            ),
        }
    )
    ordered: list[LockedPluginDependency] = []
    added: set[str] = set()
    failures: list[tuple[str, PluginDependencyError]] = []
    for root in roots:
        try:
            graph = resolve_plugin_dependencies(
                state,
                roots={root},
                builtin_contracts=builtin_contracts,
            )
        except PluginDependencyError as exc:
            failures.append((root, exc))
            continue
        for node in graph.nodes:
            if node.plugin_id not in added:
                ordered.append(node)
                added.add(node.plugin_id)
    return PluginActivationPlan(PluginLockGraph(tuple(ordered)), tuple(failures))


def resolve_plugin_dependencies(
    state: PluginStateDocument,
    roots: set[str] | None = None,
    *,
    builtin_contracts: Mapping[str, BuiltinPluginContract] | None = None,
) -> PluginLockGraph:
    builtin_contracts = builtin_contracts or {}
    requested = (
        roots
        if roots is not None
        else {
            *(
                plugin_id
                for plugin_id, record in state.plugins.items()
                if record.activation.desired_enabled
            ),
            *(
                plugin_id
                for plugin_id, contract in builtin_contracts.items()
                if builtin_desired_enabled(state, contract)
            ),
        }
    )
    selected_versions: dict[str, str] = {}
    visiting: list[str] = []
    visited: set[str] = set()
    ordered: list[LockedPluginDependency] = []

    def visit(plugin_id: str, requirement: str | None = None) -> None:
        if plugin_id in visiting:
            cycle = " -> ".join([*visiting, plugin_id])
            raise PluginDependencyError(f"plugin dependency cycle: {cycle}")
        existing_version = selected_versions.get(plugin_id)
        if existing_version is not None:
            if requirement is not None and not version_satisfies(existing_version, requirement):
                raise PluginDependencyError(
                    f"plugin dependency conflict for {plugin_id}: "
                    f"{existing_version} does not satisfy {requirement}"
                )
            return
        record = state.plugins.get(plugin_id)
        builtin = builtin_contracts.get(plugin_id)
        if record is not None and builtin is not None:
            raise PluginDependencyError(
                f"builtin plugin identity is also installed as a user plugin: {plugin_id}"
            )
        if builtin is not None:
            if requirement is not None and not version_satisfies(builtin.version, requirement):
                raise PluginDependencyError(
                    f"builtin {plugin_id} {builtin.version} does not satisfy {requirement}"
                )
            selected_versions[plugin_id] = builtin.version
            visiting.append(plugin_id)
            builtin_dependency_ids: list[str] = []
            for dependency_id, dependency_range in sorted(builtin.dependencies):
                visit(dependency_id, dependency_range)
                builtin_dependency_ids.append(dependency_id)
            visiting.pop()
            if plugin_id not in visited:
                visited.add(plugin_id)
                ordered.append(
                    LockedPluginDependency(
                        plugin_id=plugin_id,
                        version=builtin.version,
                        plugin_class="builtin",
                        package_digest=None,
                        contract_hash=builtin.contract_hash,
                        dependencies=tuple(builtin_dependency_ids),
                    )
                )
            return
        if record is None or not record.installs:
            raise PluginDependencyError(f"required plugin is not installed: {plugin_id}")
        chosen = record.selected()
        if chosen is None:
            raise PluginDependencyError(f"required plugin has no selected install: {plugin_id}")
        if requirement is not None and not version_satisfies(chosen.version, requirement):
            raise PluginDependencyError(
                f"selected {plugin_id} {chosen.version} does not satisfy {requirement}"
            )
        selected_versions[plugin_id] = chosen.version
        visiting.append(plugin_id)
        dependency_ids: list[str] = []
        for dependency in sorted(
            chosen.manifest.dependencies.plugins,
            key=lambda item: item.plugin_id,
        ):
            visit(dependency.plugin_id, dependency.version)
            dependency_ids.append(dependency.plugin_id)
        visiting.pop()
        if plugin_id not in visited:
            visited.add(plugin_id)
            ordered.append(
                LockedPluginDependency(
                    plugin_id=plugin_id,
                    version=chosen.version,
                    plugin_class="user",
                    package_digest=chosen.package_digest,
                    dependencies=tuple(dependency_ids),
                )
            )

    for plugin_id in sorted(requested):
        visit(plugin_id)
    return PluginLockGraph(tuple(ordered))


def active_dependents(
    state: PluginStateDocument,
    plugin_id: str,
    *,
    builtin_contracts: Mapping[str, BuiltinPluginContract] | None = None,
) -> list[str]:
    dependents: list[str] = []
    for candidate_id, record in state.plugins.items():
        if not record.activation.desired_enabled:
            continue
        installed = record.selected()
        if installed is None:
            continue
        if any(
            dependency.plugin_id == plugin_id
            for dependency in installed.manifest.dependencies.plugins
        ):
            dependents.append(candidate_id)
    for candidate_id, contract in (builtin_contracts or {}).items():
        if not builtin_desired_enabled(state, contract):
            continue
        if any(dependency_id == plugin_id for dependency_id, _ in contract.dependencies):
            dependents.append(candidate_id)
    return sorted(set(dependents))


def desired_dependant_closure(
    state: PluginStateDocument,
    roots: set[str],
    *,
    builtin_contracts: Mapping[str, BuiltinPluginContract] | None = None,
) -> set[str]:
    """Return roots plus every transitively desired plugin that depends on them."""

    affected = set(roots)
    changed = True
    while changed:
        changed = False
        for candidate_id, record in state.plugins.items():
            if candidate_id in affected or not record.activation.desired_enabled:
                continue
            installed = record.selected()
            if installed is None:
                continue
            dependencies = {
                dependency.plugin_id for dependency in installed.manifest.dependencies.plugins
            }
            if dependencies & affected:
                affected.add(candidate_id)
                changed = True
        for candidate_id, contract in (builtin_contracts or {}).items():
            if candidate_id in affected or not builtin_desired_enabled(state, contract):
                continue
            if {dependency_id for dependency_id, _ in contract.dependencies} & affected:
                affected.add(candidate_id)
                changed = True
    return affected


def builtin_desired_enabled(
    state: PluginStateDocument,
    contract: BuiltinPluginContract,
) -> bool:
    override = state.builtin_activation_overrides.get(contract.plugin_id)
    return override.desired_enabled if override is not None else contract.default_enabled
