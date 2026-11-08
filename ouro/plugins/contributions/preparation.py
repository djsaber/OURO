"""Coordinate contribution validation and preparation."""

from __future__ import annotations

from collections.abc import Mapping

from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.context_providers import ContextProviderRegistry
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.extensions import PluginSource
from ouro.core.observers import ObserverRegistry
from ouro.core.prompt_policies import PromptPolicyRegistry
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.turn_policies import TurnPolicyRegistry
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
from ouro.plugins.registry import PluginCommandRegistry
from ouro.skills.models import SkillMetadata
from ouro.tools.base import Tool
from ouro.tools.registry import ToolRegistry


class ContributionCoordinator:
    """Validate one generation against stable Host contribution contracts."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        observers: ObserverRegistry,
        context_providers: ContextProviderRegistry | None = None,
        commands: PluginCommandRegistry | None = None,
        compaction: CompactionStrategyRegistry | None = None,
        continuations: RunContinuationRegistry | None = None,
        prompt_policies: PromptPolicyRegistry | None = None,
        turn_policies: TurnPolicyRegistry | None = None,
        tool_policies: ToolPolicyRegistry | None = None,
    ) -> None:
        self.tools = tools
        self.observers = observers
        self.context_providers = context_providers or ContextProviderRegistry()
        self.commands = commands
        self.compaction = compaction
        self.continuations = continuations
        self.prompt_policies = prompt_policies
        self.turn_policies = turn_policies
        self.tool_policies = tool_policies

    def prepare(
        self,
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
            builtin_contract_hash=candidate.builtin_contract_hash,
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
