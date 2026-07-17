from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from ouro.core.compaction import CompactionStrategy
from ouro.core.continuations import ContinuationPolicyRegistration
from ouro.core.hooks import HookRegistration
from ouro.core.tool_policies import ToolPolicyRegistration
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.registry import PluginCommand
from ouro.tools import Tool


class ContractBackgroundService(Protocol):
    @property
    def service_id(self) -> str: ...


@dataclass(frozen=True)
class ContributionContractContext:
    manifest: PluginManifest
    tools: tuple[Tool, ...]
    hooks: tuple[HookRegistration, ...]
    commands: tuple[PluginCommand, ...]
    compaction: tuple[CompactionStrategy, ...]
    background_services: tuple[ContractBackgroundService, ...]
    continuation_policies: tuple[ContinuationPolicyRegistration, ...]
    tool_policies: tuple[ToolPolicyRegistration, ...]
    resource_contracts: dict[str, object]


class ContributionContractAdapter(Protocol):
    @property
    def kind(self) -> str: ...

    def contracts(self, context: ContributionContractContext) -> dict[str, object]: ...


@dataclass(frozen=True)
class _ToolContractAdapter:
    kind: str = "tools"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/tools/"
            f"{tool.definition.metadata['contribution_id']}": tool.definition.model_dump(
                mode="json",
                exclude={"metadata"},
            )
            for tool in context.tools
        }


@dataclass(frozen=True)
class _HookContractAdapter:
    kind: str = "hooks"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        contracts: dict[str, object] = {}
        for hook in context.hooks:
            if hook.hook_id.startswith(("skill:", "context:")):
                continue
            contracts[f"{context.manifest.plugin_id}/hooks/{hook.hook_id}"] = {
                "event": hook.hook_name,
                "priority": hook.priority,
            }
        return contracts


@dataclass(frozen=True)
class _ContextContractAdapter:
    kind: str = "context_providers"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/context_providers/"
            f"{hook.hook_id.removeprefix('context:')}": {
                "event": hook.hook_name,
                "priority": hook.priority,
            }
            for hook in context.hooks
            if hook.hook_id.startswith("context:")
        }


@dataclass(frozen=True)
class _CommandContractAdapter:
    kind: str = "commands"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/commands/{command.name}": {
                "description": command.description
            }
            for command in context.commands
        }


@dataclass(frozen=True)
class _CompactionContractAdapter:
    kind: str = "compaction_strategies"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        prefix = f"{context.manifest.plugin_id}."
        return {
            f"{context.manifest.plugin_id}/compaction_strategies/"
            f"{strategy.strategy_id.removeprefix(prefix)}": {"version": strategy.version}
            for strategy in context.compaction
        }


@dataclass(frozen=True)
class _BackgroundServiceContractAdapter:
    kind: str = "background_services"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/background_services/{service.service_id}": {
                "lifecycle": "activation-singleton"
            }
            for service in context.background_services
        }


@dataclass(frozen=True)
class _ContinuationContractAdapter:
    kind: str = "continuation_policies"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/continuation_policies/{policy.policy_id}": {
                "priority": policy.priority,
                "timeout_seconds": policy.timeout_seconds,
            }
            for policy in context.continuation_policies
        }


@dataclass(frozen=True)
class _ToolPolicyContractAdapter:
    kind: str = "tool_policies"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/tool_policies/{policy.policy_id}": {
                "priority": policy.priority,
                "timeout_seconds": policy.timeout_seconds,
                "restriction_only": True,
            }
            for policy in context.tool_policies
        }


TOOL_CONTRACT_ADAPTER = _ToolContractAdapter()
HOOK_CONTRACT_ADAPTER = _HookContractAdapter()
COMMAND_CONTRACT_ADAPTER = _CommandContractAdapter()
CONTEXT_CONTRACT_ADAPTER = _ContextContractAdapter()
COMPACTION_CONTRACT_ADAPTER = _CompactionContractAdapter()
BACKGROUND_SERVICE_CONTRACT_ADAPTER = _BackgroundServiceContractAdapter()
CONTINUATION_CONTRACT_ADAPTER = _ContinuationContractAdapter()
TOOL_POLICY_CONTRACT_ADAPTER = _ToolPolicyContractAdapter()


def build_contribution_contract_hashes(
    context: ContributionContractContext,
) -> dict[str, str]:
    from ouro.plugins.contribution_types import contribution_type_adapters

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


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ContributionContractAdapter",
    "ContributionContractContext",
    "build_contribution_contract_hashes",
]
