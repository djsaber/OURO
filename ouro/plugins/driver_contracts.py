from __future__ import annotations

from collections.abc import Mapping

from ouro.core.run_drivers import RunDriverBinding
from ouro.plugins.dependencies import (
    BuiltinPluginContract,
    PluginLockGraph,
    builtin_desired_enabled,
    resolve_plugin_dependencies,
)
from ouro.plugins.models import PluginStateDocument


def resolve_driver_contract(
    binding: RunDriverBinding,
    current: PluginStateDocument,
    builtin_contracts: Mapping[str, BuiltinPluginContract],
) -> tuple[PluginStateDocument, PluginLockGraph]:
    """Resolve persisted references without changing the published plugin selection."""

    if binding.implementation is None:
        raise ValueError("the native Run driver does not require plugin loading")
    state = current.model_copy(deep=True)
    for implementation in binding.implementations:
        plugin_id = implementation.plugin_id
        if implementation.plugin_class == "builtin":
            builtin = builtin_contracts.get(plugin_id)
            if builtin is None or builtin.contract_hash != implementation.release_id:
                raise ValueError(f"Run driver builtin contract is unavailable: {plugin_id}")
            if not builtin_desired_enabled(current, builtin):
                raise ValueError(f"Run driver implementation is disabled: {plugin_id}")
            continue
        record = state.plugins.get(plugin_id)
        if record is None or not any(
            item.package_digest == implementation.release_id for item in record.installs
        ):
            raise ValueError(f"Run driver package is unavailable: {plugin_id}")
        if not record.activation.desired_enabled:
            raise ValueError(f"Run driver implementation is disabled: {plugin_id}")
        activation = record.activation
        if implementation.configuration_revision_id not in activation.configuration_revisions:
            raise ValueError(f"Run driver configuration revision is unavailable: {plugin_id}")
        activation.selected_digest = implementation.release_id
        activation.configuration_revision_by_digest[implementation.release_id] = (
            implementation.configuration_revision_id
        )
        activation.active_configuration_revision_id = implementation.configuration_revision_id
    graph = resolve_plugin_dependencies(
        state, roots={binding.implementation.plugin_id}, builtin_contracts=builtin_contracts
    )
    if set(graph.activation_order) != {item.plugin_id for item in binding.implementations}:
        raise ValueError("Run driver dependency closure does not match its saved contract")
    return state, graph
