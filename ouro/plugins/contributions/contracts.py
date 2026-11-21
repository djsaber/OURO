"""Build persisted implementation contracts from contributions."""

from __future__ import annotations

from collections.abc import Mapping

from ouro.plugins.contributions.contract_adapters import _hash
from ouro.plugins.contributions.models import PreparedContributionSet
from ouro.plugins.manifest import PluginManifest


def build_contribution_contract_hashes(
    manifest: PluginManifest,
    prepared: PreparedContributionSet,
    resource_contracts: Mapping[str, object],
) -> dict[str, str]:
    from ouro.plugins.contributions.inventory import contribution_type_adapters

    contracts: dict[str, object] = {}
    for contribution_type in contribution_type_adapters():
        adapter = contribution_type.contract
        if adapter is None:
            continue
        for identity, value in adapter.contracts(manifest, prepared).items():
            if identity in contracts:
                raise ValueError(f"duplicate contribution contract identity: {identity}")
            contracts[identity] = value
    for identity, value in resource_contracts.items():
        if identity in contracts:
            raise ValueError(f"duplicate contribution contract identity: {identity}")
        contracts[identity] = value
    return {identity: _hash(value) for identity, value in contracts.items()}
