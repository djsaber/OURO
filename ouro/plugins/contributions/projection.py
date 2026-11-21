"""Project prepared contributions into a catalog."""

from __future__ import annotations

from ouro.plugins.catalog.gates import GenerationGate
from ouro.plugins.contributions.models import PreparedContributionSet
from ouro.plugins.contributions.projection_adapters import CatalogProjectionTargets


def project_prepared_contributions(
    targets: CatalogProjectionTargets,
    *,
    owner: str,
    prepared: PreparedContributionSet,
    gate: GenerationGate,
) -> None:
    from ouro.plugins.contributions.inventory import contribution_type_adapters

    for contribution_type in contribution_type_adapters():
        adapter = contribution_type.projection
        if adapter.kind == contribution_type.kind:
            adapter.project(targets, owner=owner, prepared=prepared, gate=gate)
