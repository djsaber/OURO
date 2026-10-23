from __future__ import annotations

from dataclasses import dataclass

from ouro.plugins.contributions.contract_adapters import (
    BACKGROUND_SERVICE_CONTRACT_ADAPTER,
    COMMAND_CONTRACT_ADAPTER,
    COMPACTION_CONTRACT_ADAPTER,
    CONTEXT_CONTRACT_ADAPTER,
    CONTINUATION_CONTRACT_ADAPTER,
    OBSERVER_CONTRACT_ADAPTER,
    PROMPT_POLICY_CONTRACT_ADAPTER,
    PROVIDER_CONTRACT_ADAPTER,
    RUN_DRIVER_CONTRACT_ADAPTER,
    SESSION_CONTROLLER_CONTRACT_ADAPTER,
    TOOL_CONTRACT_ADAPTER,
    TOOL_POLICY_CONTRACT_ADAPTER,
    TURN_POLICY_CONTRACT_ADAPTER,
    ContributionContractAdapter,
)
from ouro.plugins.contributions.preparation_adapters import (
    BACKGROUND_SERVICE_PREPARATION_ADAPTER,
    COMMAND_PREPARATION_ADAPTER,
    COMPACTION_PREPARATION_ADAPTER,
    CONTEXT_PREPARATION_ADAPTER,
    CONTINUATION_PREPARATION_ADAPTER,
    MCP_PREPARATION_ADAPTER,
    OBSERVER_PREPARATION_ADAPTER,
    PROMPT_POLICY_PREPARATION_ADAPTER,
    PROVIDER_PREPARATION_ADAPTER,
    RUN_DRIVER_PREPARATION_ADAPTER,
    SESSION_CONTROLLER_PREPARATION_ADAPTER,
    SKILL_PREPARATION_ADAPTER,
    TOOL_POLICY_PREPARATION_ADAPTER,
    TOOL_PREPARATION_ADAPTER,
    TURN_POLICY_PREPARATION_ADAPTER,
    ContributionPreparationAdapter,
)
from ouro.plugins.contributions.projection_adapters import (
    BACKGROUND_SERVICE_PROJECTION_ADAPTER,
    COMMAND_PROJECTION_ADAPTER,
    COMPACTION_PROJECTION_ADAPTER,
    CONTEXT_PROJECTION_ADAPTER,
    CONTINUATION_PROJECTION_ADAPTER,
    OBSERVER_PROJECTION_ADAPTER,
    PROMPT_POLICY_PROJECTION_ADAPTER,
    PROVIDER_PROJECTION_ADAPTER,
    RUN_DRIVER_PROJECTION_ADAPTER,
    SESSION_CONTROLLER_PROJECTION_ADAPTER,
    SKILL_PROJECTION_ADAPTER,
    TOOL_POLICY_PROJECTION_ADAPTER,
    TOOL_PROJECTION_ADAPTER,
    TURN_POLICY_PROJECTION_ADAPTER,
    ContributionLifecycle,
    ContributionProjectionAdapter,
)
from ouro.plugins.inprocess.adapters import (
    BACKGROUND_SERVICE_SDK_ADAPTER,
    COMMAND_SDK_ADAPTER,
    COMPACTION_SDK_ADAPTER,
    CONTEXT_SDK_ADAPTER,
    CONTINUATION_SDK_ADAPTER,
    OBSERVER_SDK_ADAPTER,
    PROMPT_POLICY_SDK_ADAPTER,
    PROVIDER_SDK_ADAPTER,
    RUN_DRIVER_SDK_ADAPTER,
    SESSION_CONTROLLER_SDK_ADAPTER,
    TOOL_POLICY_SDK_ADAPTER,
    TOOL_SDK_ADAPTER,
    TURN_POLICY_SDK_ADAPTER,
    SDKContributionAdapter,
)
from ouro.plugins.lifecycle.generation_resources import (
    MCP_RESOURCE_ADAPTER,
    SKILL_RESOURCE_ADAPTER,
    GenerationResourceAdapter,
)
from ouro.plugins.manifest import PluginCapability, PluginContributes
from ouro.plugins.worker.adapters import (
    BACKGROUND_SERVICE_WORKER_ADAPTER,
    COMMAND_WORKER_ADAPTER,
    COMPACTION_WORKER_ADAPTER,
    CONTEXT_WORKER_ADAPTER,
    CONTINUATION_WORKER_ADAPTER,
    OBSERVER_WORKER_ADAPTER,
    PROMPT_POLICY_WORKER_ADAPTER,
    PROVIDER_WORKER_ADAPTER,
    RUN_DRIVER_WORKER_ADAPTER,
    SESSION_CONTROLLER_WORKER_ADAPTER,
    TOOL_POLICY_WORKER_ADAPTER,
    TOOL_WORKER_ADAPTER,
    TURN_POLICY_WORKER_ADAPTER,
    WorkerContributionAdapter,
)


@dataclass(frozen=True)
class ContributionTypeAdapter:
    """Closed Host composition record for one stable contribution type."""

    kind: PluginCapability
    lifecycle: ContributionLifecycle
    preparation: ContributionPreparationAdapter
    projection: ContributionProjectionAdapter
    sdk: SDKContributionAdapter | None = None
    worker: WorkerContributionAdapter | None = None
    contract: ContributionContractAdapter | None = None
    resource: GenerationResourceAdapter | None = None


_CONTRIBUTION_TYPE_ADAPTERS: tuple[ContributionTypeAdapter, ...] = (
    ContributionTypeAdapter(
        "providers",
        "snapshot-bound",
        PROVIDER_PREPARATION_ADAPTER,
        PROVIDER_PROJECTION_ADAPTER,
        sdk=PROVIDER_SDK_ADAPTER,
        worker=PROVIDER_WORKER_ADAPTER,
        contract=PROVIDER_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "run_drivers",
        "snapshot-bound",
        RUN_DRIVER_PREPARATION_ADAPTER,
        RUN_DRIVER_PROJECTION_ADAPTER,
        sdk=RUN_DRIVER_SDK_ADAPTER,
        worker=RUN_DRIVER_WORKER_ADAPTER,
        contract=RUN_DRIVER_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "tools",
        "snapshot-bound",
        TOOL_PREPARATION_ADAPTER,
        TOOL_PROJECTION_ADAPTER,
        sdk=TOOL_SDK_ADAPTER,
        worker=TOOL_WORKER_ADAPTER,
        contract=TOOL_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "observers",
        "snapshot-bound",
        OBSERVER_PREPARATION_ADAPTER,
        OBSERVER_PROJECTION_ADAPTER,
        sdk=OBSERVER_SDK_ADAPTER,
        worker=OBSERVER_WORKER_ADAPTER,
        contract=OBSERVER_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "commands",
        "snapshot-bound",
        COMMAND_PREPARATION_ADAPTER,
        COMMAND_PROJECTION_ADAPTER,
        sdk=COMMAND_SDK_ADAPTER,
        worker=COMMAND_WORKER_ADAPTER,
        contract=COMMAND_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "context_providers",
        "snapshot-bound",
        CONTEXT_PREPARATION_ADAPTER,
        CONTEXT_PROJECTION_ADAPTER,
        sdk=CONTEXT_SDK_ADAPTER,
        worker=CONTEXT_WORKER_ADAPTER,
        contract=CONTEXT_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "compaction_strategies",
        "snapshot-bound",
        COMPACTION_PREPARATION_ADAPTER,
        COMPACTION_PROJECTION_ADAPTER,
        sdk=COMPACTION_SDK_ADAPTER,
        worker=COMPACTION_WORKER_ADAPTER,
        contract=COMPACTION_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "skills",
        "snapshot-bound",
        SKILL_PREPARATION_ADAPTER,
        SKILL_PROJECTION_ADAPTER,
        resource=SKILL_RESOURCE_ADAPTER,
    ),
    ContributionTypeAdapter(
        "mcp_servers",
        "snapshot-bound",
        MCP_PREPARATION_ADAPTER,
        TOOL_PROJECTION_ADAPTER,
        resource=MCP_RESOURCE_ADAPTER,
    ),
    ContributionTypeAdapter(
        "background_services",
        "activation-singleton",
        BACKGROUND_SERVICE_PREPARATION_ADAPTER,
        BACKGROUND_SERVICE_PROJECTION_ADAPTER,
        sdk=BACKGROUND_SERVICE_SDK_ADAPTER,
        worker=BACKGROUND_SERVICE_WORKER_ADAPTER,
        contract=BACKGROUND_SERVICE_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "continuation_policies",
        "snapshot-bound",
        CONTINUATION_PREPARATION_ADAPTER,
        CONTINUATION_PROJECTION_ADAPTER,
        sdk=CONTINUATION_SDK_ADAPTER,
        worker=CONTINUATION_WORKER_ADAPTER,
        contract=CONTINUATION_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "prompt_policies",
        "snapshot-bound",
        PROMPT_POLICY_PREPARATION_ADAPTER,
        PROMPT_POLICY_PROJECTION_ADAPTER,
        sdk=PROMPT_POLICY_SDK_ADAPTER,
        worker=PROMPT_POLICY_WORKER_ADAPTER,
        contract=PROMPT_POLICY_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "turn_policies",
        "snapshot-bound",
        TURN_POLICY_PREPARATION_ADAPTER,
        TURN_POLICY_PROJECTION_ADAPTER,
        sdk=TURN_POLICY_SDK_ADAPTER,
        worker=TURN_POLICY_WORKER_ADAPTER,
        contract=TURN_POLICY_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "tool_policies",
        "snapshot-bound",
        TOOL_POLICY_PREPARATION_ADAPTER,
        TOOL_POLICY_PROJECTION_ADAPTER,
        sdk=TOOL_POLICY_SDK_ADAPTER,
        worker=TOOL_POLICY_WORKER_ADAPTER,
        contract=TOOL_POLICY_CONTRACT_ADAPTER,
    ),
    ContributionTypeAdapter(
        "session_controllers",
        "snapshot-bound",
        SESSION_CONTROLLER_PREPARATION_ADAPTER,
        SESSION_CONTROLLER_PROJECTION_ADAPTER,
        sdk=SESSION_CONTROLLER_SDK_ADAPTER,
        worker=SESSION_CONTROLLER_WORKER_ADAPTER,
        contract=SESSION_CONTROLLER_CONTRACT_ADAPTER,
    ),
)


def contribution_type_adapters() -> tuple[ContributionTypeAdapter, ...]:
    """Return the immutable Host-owned contribution type inventory."""

    return _CONTRIBUTION_TYPE_ADAPTERS


def _validate_adapter_inventory() -> None:
    expected = tuple(PluginContributes().by_kind())
    actual = tuple(adapter.kind for adapter in _CONTRIBUTION_TYPE_ADAPTERS)
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise RuntimeError(
            "contribution adapter inventory does not match manifest capabilities: "
            f"expected={expected}, actual={actual}"
        )
    for adapter in _CONTRIBUTION_TYPE_ADAPTERS:
        if adapter.preparation.kind != adapter.kind:
            raise RuntimeError(f"preparation adapter kind mismatch: {adapter.kind}")
        if adapter.lifecycle != adapter.projection.lifecycle:
            raise RuntimeError(f"projection lifecycle mismatch: {adapter.kind}")
        if adapter.resource is None and (adapter.sdk is None or adapter.worker is None):
            raise RuntimeError(f"callable contribution adapter is incomplete: {adapter.kind}")
        if adapter.resource is not None and adapter.resource.kind != adapter.kind:
            raise RuntimeError(f"resource adapter kind mismatch: {adapter.kind}")
        if adapter.contract is None and adapter.resource is None:
            raise RuntimeError(f"contribution contract adapter is missing: {adapter.kind}")


_validate_adapter_inventory()


__all__ = [
    "ContributionTypeAdapter",
    "contribution_type_adapters",
]
