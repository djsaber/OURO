"""Coordinate contribution validation and preparation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ouro.core.extensions import PluginSource
from ouro.plugins.contributions.contracts import build_contribution_contract_hashes
from ouro.plugins.contributions.models import (
    ContributionOwner,
    PluginContributions,
    PreparedContributionSet,
)
from ouro.plugins.contributions.preparation_rules import _PreparationContext
from ouro.plugins.lifecycle.candidates import PluginRuntimeCandidate
from ouro.plugins.models import PluginGenerationRecord
from ouro.skills.models import SkillMetadata
from ouro.tools.base import Tool


def prepare_contributions(
    candidate: PluginRuntimeCandidate,
    generation: PluginGenerationRecord,
    contributions: PluginContributions,
    *,
    resource_skills: tuple[SkillMetadata, ...] = (),
    discovered_tools: tuple[Tool, ...] = (),
    resource_contracts: Mapping[str, object] | None = None,
) -> PreparedContributionSet:
    from ouro.plugins.contributions.inventory import contribution_type_adapters

    manifest = candidate.manifest
    owner = ContributionOwner(
        plugin_id=candidate.plugin_id,
        plugin_class=candidate.plugin_class,
        release_id=candidate.release_id,
        generation_id=generation.generation_id,
        package_digest=candidate.package_digest,
    )
    source: PluginSource = "official" if candidate.plugin_class == "builtin" else "third_party"
    context = _PreparationContext(
        manifest=manifest,
        contributions=contributions,
        owner=owner,
        source=source,
        resource_skills=resource_skills,
        discovered_tools=discovered_tools,
    )
    adapters = tuple(
        entry.preparation
        for entry in contribution_type_adapters()
        if entry.kind == entry.preparation.kind
    )
    items: dict[str, Any] = {}
    for adapter in adapters:
        adapter.prepare(context, items)
    prepared = PreparedContributionSet(owner=owner, **items)
    hashes = build_contribution_contract_hashes(manifest, prepared, resource_contracts or {})
    degraded_reasons = tuple(
        sorted(
            reason for adapter in adapters for reason in adapter.missing_optional(context, prepared)
        )
    )
    return replace(prepared, contract_hashes=hashes, degraded_reasons=degraded_reasons)
