"""Build persisted implementation contracts from contributions."""

from __future__ import annotations

from ouro.plugins.contributions.contract_adapters import ContributionContractContext, _hash


def build_contribution_contract_hashes(
    context: ContributionContractContext,
) -> dict[str, str]:
    from ouro.plugins.contributions.inventory import contribution_type_adapters

    contracts: dict[str, object] = {}
    for contribution_type in contribution_type_adapters():
        adapter = contribution_type.contract
        if adapter is None:
            continue
        for identity, value in adapter.contracts(context).items():
            if identity in contracts:
                raise ValueError(f"duplicate contribution contract identity: {identity}")
            contracts[identity] = value
    for identity, value in context.resource_contracts.items():
        if identity in contracts:
            raise ValueError(f"duplicate contribution contract identity: {identity}")
        contracts[identity] = value
    return {identity: _hash(value) for identity, value in contracts.items()}
