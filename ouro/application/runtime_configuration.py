from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ouro.application.models import AdapterBuilder, ConfiguredModelCatalog
from ouro.config import (
    CompactionConfig,
    CompactionConfigManager,
    PermissionConfigManager,
    PromptConfigManager,
    ProviderConfigManager,
    ProviderRuntimeOptions,
    QueuedInputConfig,
    QueuedInputConfigManager,
    RunBudgetConfigManager,
    ToolExecutionConfig,
    ToolExecutionConfigManager,
)
from ouro.core.adapter import RuntimeAdapter
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.tools import PermissionMode
from ouro.skills import SkillMetadata, discover_skill_catalog

RuntimeProviderOptions = ProviderRuntimeOptions


@dataclass(frozen=True)
class RuntimeConfigurationSnapshot:
    provider_options: ProviderRuntimeOptions
    system_prompt: str
    permission_overrides: dict[str, PermissionMode]
    compaction: CompactionConfig
    tool_execution: ToolExecutionConfig
    run_budget: RunBudgetPolicy
    queued_inputs: QueuedInputConfig
    skills: tuple[SkillMetadata, ...]


class RuntimeConfigurationResolver:
    """Resolve persisted configuration for one newly assembled runtime."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = ProviderConfigManager(root).root
        self._providers = ProviderConfigManager(self.root)
        self._prompts = PromptConfigManager(self.root)
        self._permissions = PermissionConfigManager(self.root)
        self._compaction = CompactionConfigManager(self.root)
        self._tool_execution = ToolExecutionConfigManager(self.root)
        self._run_budget = RunBudgetConfigManager(self.root)
        self._queued_inputs = QueuedInputConfigManager(self.root)

    def snapshot(
        self,
        *,
        provider_options: ProviderRuntimeOptions | None = None,
        system_prompt: str | None = None,
    ) -> RuntimeConfigurationSnapshot:
        skill_catalog = discover_skill_catalog(home=self.root)
        return RuntimeConfigurationSnapshot(
            provider_options=self._providers.resolve_runtime_options(
                provider_options or ProviderRuntimeOptions()
            ),
            system_prompt=self._prompts.resolve_system_prompt(system_prompt),
            permission_overrides=self._permissions.snapshot().tools,
            compaction=self._compaction.snapshot(),
            tool_execution=self._tool_execution.snapshot(),
            run_budget=self._run_budget.snapshot(),
            queued_inputs=self._queued_inputs.snapshot(),
            skills=tuple(skill.model_copy(deep=True) for skill in skill_catalog.skills),
        )

    def model_catalog(
        self,
        default_adapter: RuntimeAdapter,
        *,
        adapter_builder: AdapterBuilder,
    ) -> ConfiguredModelCatalog:
        return ConfiguredModelCatalog(
            self._providers,
            default_adapter,
            adapter_builder=adapter_builder,
        )

    def compaction_provider_options(
        self,
        config: CompactionConfig,
    ) -> ProviderRuntimeOptions | None:
        model = config.model
        if not model.has_dedicated_endpoint:
            return None
        assert model.provider_id is not None
        assert model.endpoint_id is not None
        return self._providers.resolve_runtime_options(
            ProviderRuntimeOptions(
                provider_id=model.provider_id,
                endpoint_id=model.endpoint_id,
            )
        )
