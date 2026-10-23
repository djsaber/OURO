"""Project prepared contributions into a catalog."""

from __future__ import annotations

from ouro.plugins.contributions.models import PreparedContributionSet
from ouro.plugins.contributions.projection_adapters import CatalogProjectionTargets


def project_prepared_contributions(
    targets: CatalogProjectionTargets,
    *,
    owner: str,
    prepared: PreparedContributionSet,
) -> None:
    from ouro.plugins.contributions.inventory import contribution_type_adapters

    projected: set[int] = set()
    for contribution_type in contribution_type_adapters():
        adapter = contribution_type.projection
        if adapter is None or id(adapter) in projected:
            continue
        projected.add(id(adapter))
        adapter.project(targets, owner=owner, prepared=prepared)
