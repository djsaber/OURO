"""Contract material for each supported contribution type."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from ouro.plugins.contributions.models import PreparedContributionSet
from ouro.plugins.manifest import PluginManifest


class ContributionContractAdapter(Protocol):
    @property
    def kind(self) -> str: ...

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class _ToolContractAdapter:
    kind: str = "tools"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            tool.definition.identity: tool.definition.execution_contract()
            for tool in prepared.tools
        }


@dataclass(frozen=True)
class _ObserverContractAdapter:
    kind: str = "observers"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/observers/{observer.observer_id}": {
                "event": observer.event,
                "event_types": list(observer.event_types),
                "priority": observer.priority,
                "timeout_seconds": observer.timeout_seconds,
                "active_when_controller_id": observer.active_when_controller_id,
            }
            for observer in prepared.observers
        }


@dataclass(frozen=True)
class _CommandContractAdapter:
    kind: str = "commands"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/commands/{command.name}": {
                "description": command.description,
                "usage": command.usage,
                "argument_options": [
                    {
                        "value": item.value,
                        "description": item.description,
                        "usage": item.usage,
                    }
                    for item in command.argument_options
                ],
                "requires_session": command.requires_session,
            }
            for command in prepared.commands
        }


@dataclass(frozen=True)
class _SessionControllerContractAdapter:
    kind: str = "session_controllers"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/session_controllers/{controller.controller_id}": {
                "claims": [claim.model_dump(mode="json") for claim in controller.claims],
                "timeout_seconds": controller.timeout_seconds,
            }
            for controller in prepared.session_controllers
        }


@dataclass(frozen=True)
class _CompactionContractAdapter:
    kind: str = "compaction_strategies"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        prefix = f"{manifest.plugin_id}."
        return {
            f"{manifest.plugin_id}/compaction_strategies/"
            f"{strategy.strategy_id.removeprefix(prefix)}": {"version": strategy.version}
            for strategy in prepared.compaction_strategies
        }


@dataclass(frozen=True)
class _BackgroundServiceContractAdapter:
    kind: str = "background_services"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/background_services/{service.service_id}": {
                "lifecycle": "activation-singleton",
                "scope": service.scope,
            }
            for service in prepared.background_services
        }


@dataclass(frozen=True)
class _ContinuationContractAdapter:
    kind: str = "continuation_policies"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/continuation_policies/{policy.policy_id}": {
                "priority": policy.priority,
                "timeout_seconds": policy.timeout_seconds,
                "active_when_controller_id": policy.active_when_controller_id,
            }
            for policy in prepared.continuation_policies
        }


@dataclass(frozen=True)
class _ToolPolicyContractAdapter:
    kind: str = "tool_policies"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/tool_policies/{policy.policy_id}": {
                "priority": policy.priority,
                "timeout_seconds": policy.timeout_seconds,
                "restriction_only": True,
                "active_when_controller_id": policy.active_when_controller_id,
            }
            for policy in prepared.tool_policies
        }


@dataclass(frozen=True)
class _PromptPolicyContractAdapter:
    kind: str = "prompt_policies"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/prompt_policies/{policy.policy_id}": {
                "priority": policy.priority,
                "timeout_seconds": policy.timeout_seconds,
                "deny_only": True,
                "active_when_controller_id": policy.active_when_controller_id,
                "permission": "sessions.prompt.inspect",
            }
            for policy in prepared.prompt_policies
        }


@dataclass(frozen=True)
class _TurnPolicyContractAdapter:
    kind: str = "turn_policies"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/turn_policies/{policy.policy_id}": {
                "priority": policy.priority,
                "timeout_seconds": policy.timeout_seconds,
                "active_when_controller_id": policy.active_when_controller_id,
            }
            for policy in prepared.turn_policies
        }


TOOL_CONTRACT_ADAPTER = _ToolContractAdapter()


OBSERVER_CONTRACT_ADAPTER = _ObserverContractAdapter()


COMMAND_CONTRACT_ADAPTER = _CommandContractAdapter()


COMPACTION_CONTRACT_ADAPTER = _CompactionContractAdapter()


BACKGROUND_SERVICE_CONTRACT_ADAPTER = _BackgroundServiceContractAdapter()


@dataclass(frozen=True)
class _RunDriverContractAdapter:
    kind: str = "run_drivers"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {item.canonical_id: {"protocol": "run-driver/1"} for item in prepared.run_drivers}


@dataclass(frozen=True)
class _ProviderContractAdapter:
    kind: str = "providers"

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {item.canonical_id: {"protocol": "model-provider/1"} for item in prepared.providers}


PROVIDER_CONTRACT_ADAPTER = _ProviderContractAdapter()


RUN_DRIVER_CONTRACT_ADAPTER = _RunDriverContractAdapter()


CONTINUATION_CONTRACT_ADAPTER = _ContinuationContractAdapter()


TOOL_POLICY_CONTRACT_ADAPTER = _ToolPolicyContractAdapter()


PROMPT_POLICY_CONTRACT_ADAPTER = _PromptPolicyContractAdapter()


TURN_POLICY_CONTRACT_ADAPTER = _TurnPolicyContractAdapter()


SESSION_CONTROLLER_CONTRACT_ADAPTER = _SessionControllerContractAdapter()


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
