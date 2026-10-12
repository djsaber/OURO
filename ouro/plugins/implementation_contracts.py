from __future__ import annotations

from ouro.core.plugin_contracts import PluginImplementation
from ouro.plugins.models import PluginGenerationRecord


def plugin_implementation(record: PluginGenerationRecord) -> PluginImplementation:
    if record.execution_contract_hash is None:
        raise ValueError(f"Plugin implementation contract is missing: {record.plugin_id}")
    return PluginImplementation(
        plugin_id=record.plugin_id,
        plugin_class=record.plugin_class,
        release_id=record.release_id,
        configuration_revision_id=record.configuration_revision_id,
        execution_contract_hash=record.execution_contract_hash,
    )
