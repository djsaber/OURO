from __future__ import annotations

import re
from collections.abc import Mapping

from ouro.application.contracts.agents import (
    AgentDefinitionCatalogView,
    AgentDefinitionDetail,
    AgentDefinitionView,
    AgentToolOptionView,
)
from ouro.config.agents import AgentDefinitionsConfig, AgentDefinitionsConfigManager
from ouro.config.providers.manager import ProviderConfigManager
from ouro.core.agents import (
    AGENT_CONTROL_TOOL_NAMES,
    AgentCatalogEntry,
    AgentCatalogSnapshot,
    AgentDefinition,
    AgentSpawnPolicy,
    EffectiveToolRef,
    ToolSelection,
)
from ouro.core.approvals import (
    PermissionMode,
    SessionApprovalMode,
    configured_tool_permission,
    resolve_tool_permission,
)
from ouro.core.base import new_id
from ouro.core.errors import ConfigError
from ouro.core.tools import tool_definition_contract_hash
from ouro.tools import ToolDefinition, ToolRegistry

GENERAL_AGENT_DEFINITION = AgentDefinition(
    agent_id="general",
    name="General",
    description="A general-purpose subagent for a bounded delegated task.",
    system_prompt=(
        "Work independently on the delegated task. Use only the available tools and return "
        "a concise result with evidence, verification, and unresolved questions."
    ),
    tool_selection=ToolSelection(mode="all"),
    can_spawn=False,
)


class AgentDefinitionService:
    """Resolve definitions and current tool capabilities for one decision."""

    def __init__(
        self, config: AgentDefinitionsConfigManager, *, spawn_policy: AgentSpawnPolicy | None = None
    ) -> None:
        self.config = config
        self._spawn_policy = spawn_policy
        self.providers = ProviderConfigManager(config.file.root)

    def default_model_route(self) -> str:
        return self.providers.require_default_route()

    def list(self, *, tools: ToolRegistry) -> AgentDefinitionCatalogView:
        config = self.config.snapshot()
        entries = self._entries(tools, config=config)
        policy = self._spawn_policy or config.runtime.spawn_policy
        return AgentDefinitionCatalogView(
            revision=config.revision,
            policy_restart_required=policy != config.runtime.spawn_policy,
            definitions=tuple(
                self._view(
                    entry,
                    enabled=entry.definition.agent_id not in config.disabled_agent_ids,
                    allowed_by_policy=policy.allows_agent(entry.definition.agent_id),
                    source=(
                        "builtin"
                        if entry.definition.agent_id == GENERAL_AGENT_DEFINITION.agent_id
                        else "user"
                    ),
                )
                for entry in entries
            ),
        )

    def get(self, agent_id: str, *, tools: ToolRegistry) -> AgentDefinitionDetail:
        config = self.config.snapshot()
        entry = next(
            (
                item
                for item in self._entries(tools, config=config)
                if item.definition.agent_id == agent_id
            ),
            None,
        )
        if entry is None:
            raise ConfigError(f"agent definition not found: {agent_id}")
        source = "builtin" if agent_id == GENERAL_AGENT_DEFINITION.agent_id else "user"
        view = self._view(
            entry,
            source=source,
            enabled=agent_id not in config.disabled_agent_ids,
            allowed_by_policy=(self._spawn_policy or config.runtime.spawn_policy).allows_agent(
                agent_id
            ),
        )
        return AgentDefinitionDetail(
            **view.model_dump(), system_prompt=entry.definition.system_prompt
        )

    def create(
        self,
        *,
        name: str,
        description: str,
        system_prompt: str,
        tool_selection: ToolSelection,
        model: str | None = None,
        tools: ToolRegistry,
        expected_revision: int,
    ) -> AgentDefinitionCatalogView:
        self._require_known_selected_tools(tool_selection, tools)
        definition = AgentDefinition(
            agent_id=self._new_agent_id(name),
            name=name,
            description=description,
            system_prompt=system_prompt,
            tool_selection=tool_selection,
            model=model,
        )
        self.config.create(definition, expected_revision=expected_revision)
        return self.list(tools=tools)

    def update(
        self,
        *,
        agent_id: str,
        name: str,
        description: str,
        system_prompt: str,
        tool_selection: ToolSelection,
        model: str | None,
        tools: ToolRegistry,
        expected_revision: int,
    ) -> AgentDefinitionCatalogView:
        if agent_id == GENERAL_AGENT_DEFINITION.agent_id:
            raise ConfigError(f"builtin agent definition is immutable: {agent_id}")
        self._require_known_selected_tools(tool_selection, tools)
        current = next(
            (item for item in self.config.snapshot().definitions if item.agent_id == agent_id),
            None,
        )
        if current is None:
            raise ConfigError(f"agent definition not found: {agent_id}")
        updated = AgentDefinition(
            agent_id=agent_id,
            name=name,
            description=description,
            system_prompt=system_prompt,
            tool_selection=tool_selection,
            model=model,
            can_spawn=current.can_spawn,
            default_budget=current.default_budget,
        )
        self.config.update(updated, expected_revision=expected_revision)
        return self.list(tools=tools)

    def delete(
        self,
        agent_id: str,
        *,
        expected_revision: int,
        tools: ToolRegistry | None = None,
    ) -> AgentDefinitionCatalogView:
        if agent_id == GENERAL_AGENT_DEFINITION.agent_id:
            raise ConfigError(f"builtin agent definition is immutable: {agent_id}")
        self.config.delete(agent_id, expected_revision=expected_revision)
        return self.list(tools=tools or ToolRegistry())

    def set_enabled(
        self, agent_id: str, *, enabled: bool, expected_revision: int, tools: ToolRegistry
    ) -> AgentDefinitionCatalogView:
        self.config.set_enabled(agent_id, enabled=enabled, expected_revision=expected_revision)
        return self.list(tools=tools)

    def copy_builtin(
        self,
        agent_id: str,
        *,
        name: str,
        tools: ToolRegistry,
        expected_revision: int,
    ) -> AgentDefinitionCatalogView:
        if agent_id != GENERAL_AGENT_DEFINITION.agent_id:
            raise ConfigError(f"builtin agent definition not found: {agent_id}")
        copied = AgentDefinition(
            **{
                **GENERAL_AGENT_DEFINITION.model_dump(exclude={"contract_hash"}),
                "agent_id": self._new_agent_id(name),
                "name": name,
            }
        )
        self.config.create(copied, expected_revision=expected_revision)
        return self.list(tools=tools)

    @staticmethod
    def _new_agent_id(name: str) -> str:
        prefix = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:31].rstrip("-")
        if not prefix or not prefix[0].isalpha():
            prefix = "agent"
        return new_id(prefix)

    def tool_options(self, tools: ToolRegistry) -> tuple[AgentToolOptionView, ...]:
        return tuple(
            AgentToolOptionView(
                name=definition.name,
                description=definition.description,
                effect=definition.effect,
                owner_id=self._owner_id(tools, definition.name),
                execution_contract_hash=self.tool_reference(
                    tools, definition
                ).execution_contract_hash,
            )
            for definition in self._candidate_definitions(tools)
        )

    def catalog_snapshot(
        self,
        tools: ToolRegistry,
        *,
        permission_overrides: Mapping[str, PermissionMode] | None = None,
        plugin_permission_overrides: Mapping[str, PermissionMode] | None = None,
        approval_mode: SessionApprovalMode = "inherit",
    ) -> AgentCatalogSnapshot:
        config = self.config.snapshot()
        return AgentCatalogSnapshot(
            revision=config.revision,
            entries=tuple(
                entry
                for entry in self._entries(
                    tools,
                    config=config,
                    permission_overrides=permission_overrides,
                    plugin_permission_overrides=plugin_permission_overrides,
                    approval_mode=approval_mode,
                )
                if entry.definition.agent_id not in config.disabled_agent_ids
            ),
        )

    def _entries(
        self,
        tools: ToolRegistry,
        *,
        config: AgentDefinitionsConfig,
        permission_overrides: Mapping[str, PermissionMode] | None = None,
        plugin_permission_overrides: Mapping[str, PermissionMode] | None = None,
        approval_mode: SessionApprovalMode = "inherit",
    ) -> tuple[AgentCatalogEntry, ...]:
        overrides = permission_overrides or {}
        plugin_overrides = plugin_permission_overrides or {}
        definitions = (GENERAL_AGENT_DEFINITION, *config.definitions)
        candidates = {
            definition.name: definition for definition in self._candidate_definitions(tools)
        }
        entries: list[AgentCatalogEntry] = []
        for definition in definitions:
            selected_names = (
                tuple(candidates)
                if definition.tool_selection.mode == "all"
                else definition.tool_selection.names
            )
            missing = tuple(name for name in selected_names if name not in candidates)
            effective: list[EffectiveToolRef] = []
            for name in selected_names:
                if name not in candidates:
                    continue
                tool = candidates[name]
                raw_plugin_id = tool.metadata.get("plugin_id")
                plugin_id = (
                    raw_plugin_id if isinstance(raw_plugin_id, str) and raw_plugin_id else None
                )
                permission = resolve_tool_permission(
                    tool.permission_mode,
                    approval_mode,
                    permission_control=tool.permission_control,
                    override=configured_tool_permission(
                        name,
                        plugin_id,
                        tool_overrides=overrides,
                        plugin_overrides=plugin_overrides,
                    ),
                )
                if permission == "deny":
                    continue
                effective.append(
                    self.tool_reference(
                        tools,
                        tool,
                        minimum_permission_mode=permission,
                    )
                )
            entries.append(
                AgentCatalogEntry(
                    definition=definition,
                    effective_tools=tuple(effective),
                    missing_tool_names=missing,
                )
            )
        return tuple(entries)

    @staticmethod
    def _candidate_definitions(tools: ToolRegistry) -> tuple[ToolDefinition, ...]:
        return tuple(
            definition
            for definition in tools.definitions()
            if definition.name not in AGENT_CONTROL_TOOL_NAMES
        )

    @classmethod
    def tool_reference(
        cls,
        tools: ToolRegistry,
        definition: ToolDefinition,
        *,
        minimum_permission_mode: PermissionMode = "auto",
    ) -> EffectiveToolRef:
        metadata = definition.metadata
        plugin_id = metadata.get("plugin_id")
        contribution_id = metadata.get("contribution_id")
        canonical_id = (
            f"{plugin_id}/tools/{contribution_id}"
            if isinstance(plugin_id, str) and isinstance(contribution_id, str)
            else f"ouro.host/tools/{definition.name}"
        )
        execution_hash = metadata.get("execution_contract_hash")
        return EffectiveToolRef(
            public_name=definition.name,
            canonical_tool_id=canonical_id,
            owner_id=cls._owner_id(tools, definition.name),
            execution_contract_hash=(
                execution_hash
                if isinstance(execution_hash, str) and execution_hash
                else tool_definition_contract_hash(definition)
            ),
            source=definition.source,
            effect=definition.effect,
            minimum_permission_mode=minimum_permission_mode,
        )

    @staticmethod
    def _owner_id(tools: ToolRegistry, name: str) -> str:
        return tools.owner_of(name) or "ouro.host"

    @staticmethod
    def _require_known_selected_tools(
        selection: ToolSelection,
        tools: ToolRegistry,
    ) -> None:
        if selection.mode != "selected":
            return
        known = set(tools.names()) - AGENT_CONTROL_TOOL_NAMES
        missing = sorted(set(selection.names) - known)
        if missing:
            raise ConfigError("unknown selected tools: " + ", ".join(missing))

    @staticmethod
    def _view(
        entry: AgentCatalogEntry,
        *,
        source: str,
        enabled: bool,
        allowed_by_policy: bool,
    ) -> AgentDefinitionView:
        return AgentDefinitionView(
            agent_id=entry.definition.agent_id,
            name=entry.definition.name,
            description=entry.definition.description,
            tool_selection=entry.definition.tool_selection,
            model=entry.definition.model,
            source="builtin" if source == "builtin" else "user",
            editable=source == "user",
            enabled=enabled,
            allowed_by_policy=allowed_by_policy,
            compatibility="available" if entry.available else "unavailable",
            missing_tool_names=entry.missing_tool_names,
            contract_hash=entry.definition.contract_hash,
        )


__all__ = ["AgentDefinitionService", "GENERAL_AGENT_DEFINITION"]
