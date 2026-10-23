"""Convert in-process SDK contributions into host records."""

from __future__ import annotations

from ouro.plugins.contributions.models import PluginContributions
from ouro.sdk.plugins import v5 as sdk


def adapt_sdk_contributions(
    contributions: sdk.ContributionSet, *, owner_id: str
) -> PluginContributions:
    from ouro.plugins.contributions.inventory import contribution_type_adapters

    adapted = PluginContributions()
    for contribution_type in contribution_type_adapters():
        adapter = contribution_type.sdk
        if adapter is None:
            continue
        source = getattr(contributions, adapter.attribute)
        setattr(
            adapted, adapter.attribute, [adapter.adapt(item, owner_id=owner_id) for item in source]
        )
    return adapted
