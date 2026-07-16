from __future__ import annotations

from dataclasses import dataclass

from ouro.plugins.models import InstalledPlugin, PluginStateDocument
from ouro.plugins.semver import version_satisfies


class PluginDependencyError(ValueError):
    pass


@dataclass(frozen=True)
class LockedPluginDependency:
    plugin_id: str
    version: str
    package_digest: str
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


def plan_desired_plugin_activation(state: PluginStateDocument) -> PluginActivationPlan:
    roots = sorted(
        plugin_id
        for plugin_id, record in state.plugins.items()
        if record.activation.desired_enabled
    )
    ordered: list[LockedPluginDependency] = []
    added: set[str] = set()
    failures: list[tuple[str, PluginDependencyError]] = []
    for root in roots:
        try:
            graph = resolve_plugin_dependencies(state, roots={root})
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
) -> PluginLockGraph:
    requested = roots or {
        plugin_id
        for plugin_id, record in state.plugins.items()
        if record.activation.desired_enabled
    }
    selected: dict[str, InstalledPlugin] = {}
    visiting: list[str] = []
    visited: set[str] = set()
    ordered: list[LockedPluginDependency] = []

    def visit(plugin_id: str, requirement: str | None = None) -> None:
        if plugin_id in visiting:
            cycle = " -> ".join([*visiting, plugin_id])
            raise PluginDependencyError(f"plugin dependency cycle: {cycle}")
        existing = selected.get(plugin_id)
        if existing is not None:
            if requirement is not None and not version_satisfies(existing.version, requirement):
                raise PluginDependencyError(
                    f"plugin dependency conflict for {plugin_id}: "
                    f"{existing.version} does not satisfy {requirement}"
                )
            return
        record = state.plugins.get(plugin_id)
        if record is None or not record.installs:
            raise PluginDependencyError(f"required plugin is not installed: {plugin_id}")
        chosen = record.selected()
        if chosen is None:
            raise PluginDependencyError(f"required plugin has no selected install: {plugin_id}")
        if requirement is not None and not version_satisfies(chosen.version, requirement):
            raise PluginDependencyError(
                f"selected {plugin_id} {chosen.version} does not satisfy {requirement}"
            )
        selected[plugin_id] = chosen
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
                    package_digest=chosen.package_digest,
                    dependencies=tuple(dependency_ids),
                )
            )

    for plugin_id in sorted(requested):
        visit(plugin_id)
    return PluginLockGraph(tuple(ordered))


def active_dependents(state: PluginStateDocument, plugin_id: str) -> list[str]:
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
    return sorted(dependents)
