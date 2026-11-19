"""Coordinate contribution validation and preparation."""

from __future__ import annotations

from collections.abc import Mapping

from ouro.core.extensions import PluginSource
from ouro.plugins.contributions.contract_adapters import ContributionContractContext
from ouro.plugins.contributions.contracts import build_contribution_contract_hashes
from ouro.plugins.contributions.models import (
    ContributionOwner,
    PluginContributions,
    PreparedContributionSet,
)
from ouro.plugins.contributions.preparation_rules import _PreparationContext, _PreparedBuilder
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
    adapters = contribution_type_adapters()
    builder = _PreparedBuilder()
    for adapter in adapters:
        adapter.preparation.prepare(context, builder)
    hashes = build_contribution_contract_hashes(
        ContributionContractContext(
            manifest=manifest,
            tools=builder.tools,
            observers=builder.observers,
            context_providers=builder.context_providers,
            system_prompt_providers=builder.system_prompt_providers,
            commands=builder.commands,
            compaction=builder.compaction_strategies,
            background_services=builder.background_services,
            continuation_policies=builder.continuation_policies,
            providers=builder.providers,
            run_drivers=builder.run_drivers,
            prompt_policies=builder.prompt_policies,
            turn_policies=builder.turn_policies,
            tool_policies=builder.tool_policies,
            session_controllers=builder.session_controllers,
            resource_contracts=dict(resource_contracts or {}),
        )
    )
    degraded_reasons = tuple(
        sorted(
            reason
            for adapter in adapters
            for reason in adapter.preparation.missing_optional(context, builder)
        )
    )
    return PreparedContributionSet(
        owner=owner,
        tools=builder.tools,
        observers=builder.observers,
        context_providers=builder.context_providers,
        system_prompt_providers=builder.system_prompt_providers,
        commands=builder.commands,
        compaction_strategies=builder.compaction_strategies,
        background_services=builder.background_services,
        skills=builder.skills,
        continuation_policies=builder.continuation_policies,
        providers=builder.providers,
        run_drivers=builder.run_drivers,
        prompt_policies=builder.prompt_policies,
        turn_policies=builder.turn_policies,
        tool_policies=builder.tool_policies,
        session_controllers=builder.session_controllers,
        contract_hashes=hashes,
        degraded_reasons=degraded_reasons,
    )
