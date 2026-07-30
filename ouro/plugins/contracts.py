from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from ouro.core.compaction import CompactionStrategy
from ouro.core.context_providers import ContextProviderRegistration
from ouro.core.continuations import ContinuationPolicyRegistration
from ouro.core.observers import ObserverRegistration
from ouro.core.prompt_policies import PromptPolicyRegistration
from ouro.core.tool_policies import ToolPolicyRegistration
from ouro.core.turn_policies import TurnPolicyRegistration
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.registry import PluginCommand
from ouro.plugins.session_controllers import PluginSessionController
from ouro.tools import Tool


class ContractBackgroundService(Protocol):
    @property
    def service_id(self) -> str: ...


@dataclass(frozen=True)
class ContributionContractContext:
    manifest: PluginManifest
    tools: tuple[Tool, ...]
    observers: tuple[ObserverRegistration, ...]
    context_providers: tuple[ContextProviderRegistration, ...]
    commands: tuple[PluginCommand, ...]
    compaction: tuple[CompactionStrategy, ...]
    background_services: tuple[ContractBackgroundService, ...]
    continuation_policies: tuple[ContinuationPolicyRegistration, ...]
    prompt_policies: tuple[PromptPolicyRegistration, ...]
    turn_policies: tuple[TurnPolicyRegistration, ...]
    tool_policies: tuple[ToolPolicyRegistration, ...]
    session_controllers: tuple[PluginSessionController, ...]
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
class _ObserverContractAdapter:
    kind: str = "observers"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/observers/{observer.observer_id}": {
                "event": observer.event,
                "event_types": list(observer.event_types),
                "priority": observer.priority,
                "timeout_seconds": observer.timeout_seconds,
                "active_when_controller_id": observer.active_when_controller_id,
            }
            for observer in context.observers
        }


@dataclass(frozen=True)
class _ContextContractAdapter:
    kind: str = "context_providers"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/context_providers/{provider.provider_id}": {
                "priority": provider.priority,
                "timeout_seconds": provider.timeout_seconds,
                "failure_mode": provider.failure_mode,
                "active_when_controller_id": provider.active_when_controller_id,
            }
            for provider in context.context_providers
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
class _SessionControllerContractAdapter:
    kind: str = "session_controllers"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/session_controllers/{controller.controller_id}": {
                "description": controller.description,
                "claims": [claim.model_dump(mode="json") for claim in controller.claims],
                "timeout_seconds": controller.timeout_seconds,
            }
            for controller in context.session_controllers
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
                "active_when_controller_id": policy.active_when_controller_id,
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
                "active_when_controller_id": policy.active_when_controller_id,
            }
            for policy in context.tool_policies
        }


@dataclass(frozen=True)
class _PromptPolicyContractAdapter:
    kind: str = "prompt_policies"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/prompt_policies/{policy.policy_id}": {
                "priority": policy.priority,
                "timeout_seconds": policy.timeout_seconds,
                "deny_only": True,
                "active_when_controller_id": policy.active_when_controller_id,
                "permission": "sessions.prompt.inspect",
            }
            for policy in context.prompt_policies
        }


@dataclass(frozen=True)
class _TurnPolicyContractAdapter:
    kind: str = "turn_policies"

    def contracts(self, context: ContributionContractContext) -> dict[str, object]:
        return {
            f"{context.manifest.plugin_id}/turn_policies/{policy.policy_id}": {
                "priority": policy.priority,
                "timeout_seconds": policy.timeout_seconds,
                "active_when_controller_id": policy.active_when_controller_id,
            }
            for policy in context.turn_policies
        }


TOOL_CONTRACT_ADAPTER = _ToolContractAdapter()
OBSERVER_CONTRACT_ADAPTER = _ObserverContractAdapter()
COMMAND_CONTRACT_ADAPTER = _CommandContractAdapter()
CONTEXT_CONTRACT_ADAPTER = _ContextContractAdapter()
COMPACTION_CONTRACT_ADAPTER = _CompactionContractAdapter()
BACKGROUND_SERVICE_CONTRACT_ADAPTER = _BackgroundServiceContractAdapter()
CONTINUATION_CONTRACT_ADAPTER = _ContinuationContractAdapter()
TOOL_POLICY_CONTRACT_ADAPTER = _ToolPolicyContractAdapter()
PROMPT_POLICY_CONTRACT_ADAPTER = _PromptPolicyContractAdapter()
TURN_POLICY_CONTRACT_ADAPTER = _TurnPolicyContractAdapter()
SESSION_CONTROLLER_CONTRACT_ADAPTER = _SessionControllerContractAdapter()


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
