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
    default_enabled: bool = True


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
    return _resolve_plugin_activation(
        state, None, builtin_contracts=builtin_contracts or {}, isolate_failures=True
    )


def resolve_plugin_dependencies(
    state: PluginStateDocument,
    roots: set[str] | None = None,
    *,
    builtin_contracts: Mapping[str, BuiltinPluginContract] | None = None,
) -> PluginLockGraph:
    return _resolve_plugin_activation(
        state, roots, builtin_contracts=builtin_contracts or {}, isolate_failures=False
    ).graph


def _resolve_plugin_activation(
    state: PluginStateDocument,
    roots: set[str] | None,
    *,
    builtin_contracts: Mapping[str, BuiltinPluginContract],
    isolate_failures: bool,
) -> PluginActivationPlan:
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
    visiting: list[str] = []
    resolved: dict[str, LockedPluginDependency] = {}
    failed: dict[str, PluginDependencyError] = {}

    def visit(plugin_id: str, requirement: str | None = None) -> None:
        if plugin_id in visiting:
            cycle = " -> ".join([*visiting, plugin_id])
            raise PluginDependencyError(f"plugin dependency cycle: {cycle}")
        existing = resolved.get(plugin_id)
        if existing is not None:
            if requirement is not None and not version_satisfies(existing.version, requirement):
                raise PluginDependencyError(
                    f"plugin dependency conflict for {plugin_id}: "
                    f"{existing.version} does not satisfy {requirement}"
                )
            return
        if plugin_id in failed:
            raise failed[plugin_id]
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
            dependencies = sorted(builtin.dependencies)
            node = LockedPluginDependency(
                plugin_id=plugin_id,
                version=builtin.version,
                plugin_class="builtin",
                package_digest=None,
                contract_hash=builtin.contract_hash,
                dependencies=tuple(item[0] for item in dependencies),
            )
        else:
            if record is None or not record.installs:
                raise PluginDependencyError(f"required plugin is not installed: {plugin_id}")
            chosen = record.selected()
            if chosen is None:
                raise PluginDependencyError(f"required plugin has no selected install: {plugin_id}")
            if requirement is not None and not version_satisfies(chosen.version, requirement):
                raise PluginDependencyError(
                    f"selected {plugin_id} {chosen.version} does not satisfy {requirement}"
                )
            dependencies = sorted(
                (item.plugin_id, item.version) for item in chosen.manifest.dependencies.plugins
            )
            node = LockedPluginDependency(
                plugin_id=plugin_id,
                version=chosen.version,
                plugin_class="user",
                package_digest=chosen.package_digest,
                dependencies=tuple(item[0] for item in dependencies),
            )
        visiting.append(plugin_id)
        try:
            for dependency_id, dependency_range in dependencies:
                visit(dependency_id, dependency_range)
        except PluginDependencyError as exc:
            failed[plugin_id] = exc
            raise
        finally:
            visiting.pop()
        resolved[plugin_id] = node

    failures: list[tuple[str, PluginDependencyError]] = []
    accepted: set[str] = set()
    for plugin_id in sorted(
        requested,
        key=lambda candidate_id: plugin_root_sort_key(candidate_id, state),
    ):
        try:
            visit(plugin_id)
            accepted.add(plugin_id)
        except PluginDependencyError as exc:
            if not isolate_failures:
                raise
            failures.append((plugin_id, exc))
    pending = list(accepted)
    while pending:
        for dependency_id in resolved[pending.pop()].dependencies:
            if dependency_id not in accepted:
                accepted.add(dependency_id)
                pending.append(dependency_id)
    graph = PluginLockGraph(tuple(node for key, node in resolved.items() if key in accepted))
    return PluginActivationPlan(graph, tuple(failures))


def plugin_root_sort_key(plugin_id: str, state: PluginStateDocument) -> tuple[int, str]:
    # User plugins are explicit additions and appear before unrelated ambient builtins.
    # The depth-first resolver still places every dependency before its dependant.
    return (0 if plugin_id in state.plugins else 1, plugin_id)


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

    dependants: dict[str, set[str]] = {}
    for candidate_id, record in state.plugins.items():
        installed = record.selected()
        if not record.activation.desired_enabled or installed is None:
            continue
        for dependency in installed.manifest.dependencies.plugins:
            dependants.setdefault(dependency.plugin_id, set()).add(candidate_id)
    for candidate_id, contract in (builtin_contracts or {}).items():
        if builtin_desired_enabled(state, contract):
            for dependency_id, _ in contract.dependencies:
                dependants.setdefault(dependency_id, set()).add(candidate_id)
    affected = set(roots)
    pending = list(roots)
    while pending:
        for candidate_id in dependants.get(pending.pop(), ()):
            if candidate_id not in affected:
                affected.add(candidate_id)
                pending.append(candidate_id)
    return affected


def builtin_desired_enabled(
    state: PluginStateDocument,
    contract: BuiltinPluginContract,
) -> bool:
    override = state.builtin_activation_overrides.get(contract.plugin_id)
    return override.desired_enabled if override is not None else contract.default_enabled
