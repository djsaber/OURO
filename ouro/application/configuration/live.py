from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.application.contracts.configuration import (
    CompactionConfigInput,
    CompactionConfigView,
    MCPServerConfigInput,
    MCPServerConfigView,
    SkillManagementView,
    SkillPolicyView,
)
from ouro.application.models.catalog import RuntimeAdapterCatalog
from ouro.core.errors import ConfigError
from ouro.core.reasoning import resolve_reasoning
from ouro.mcp.models import MCPServerConfig
from ouro.skills.models import SkillMetadata


class LiveConfigurationService:
    """Apply persisted configuration to a running environment with its compensation rules."""

    def __init__(
        self,
        configuration: ConfigurationFacade,
        *,
        reconcile_mcp: Callable[[MCPServerConfig], Awaitable[object]],
        refresh_skills: Callable[[tuple[SkillMetadata, ...]], Awaitable[None]],
        available_skills: Callable[[], Awaitable[tuple[SkillMetadata, ...]]],
    ) -> None:
        self.configuration = configuration
        self._reconcile_mcp = reconcile_mcp
        self._refresh_skills = refresh_skills
        self._available_skills = available_skills

    async def save_compaction(
        self, draft: CompactionConfigInput, *, models: RuntimeAdapterCatalog
    ) -> CompactionConfigView:
        """Validate the live selection before publishing the persisted configuration."""

        def save() -> CompactionConfigView:
            model = None
            if draft.use_default_model:
                model = models.configured_default_model()
            elif draft.provider_id is not None and draft.endpoint_id is not None:
                model = models.resolve(f"{draft.provider_id}/{draft.endpoint_id}")
            if model is not None:
                models.adapter_for(model.key)
                if draft.reasoning is not None:
                    try:
                        resolve_reasoning(model.reasoning, draft.reasoning)
                    except ValueError as exc:
                        raise ConfigError(f"invalid compaction reasoning override: {exc}") from exc
            return self.configuration.save_compaction(draft)

        return await asyncio.to_thread(save)

    async def set_mcp_enabled(
        self,
        server_id: str,
        *,
        enabled: bool,
    ) -> MCPServerConfigView:
        configuration = self.configuration
        previous = await asyncio.to_thread(configuration.mcp_server, server_id)
        if previous.enabled == enabled:
            return previous

        def update() -> tuple[MCPServerConfigView, MCPServerConfig]:
            saved = configuration.save_mcp_server(
                server_id,
                MCPServerConfigInput(enabled=enabled),
            )
            config = configuration.mcp.get(server_id)
            assert config is not None
            return saved, config

        saved, config = await asyncio.to_thread(update)
        try:
            await self._reconcile_mcp(config)
        except Exception:
            await asyncio.to_thread(
                configuration.save_mcp_server,
                server_id,
                MCPServerConfigInput(enabled=previous.enabled),
            )
            restored = configuration.mcp.get(server_id)
            assert restored is not None
            await self._reconcile_mcp(restored)
            raise
        return saved

    async def save_mcp_configuration(
        self,
        server_id: str | None,
        update: MCPServerConfigInput,
    ) -> MCPServerConfigView:
        configuration = self.configuration
        previous = (
            await asyncio.to_thread(configuration.mcp.get, server_id)
            if server_id is not None
            else None
        )

        def save() -> tuple[MCPServerConfigView, MCPServerConfig]:
            saved = configuration.save_mcp_server(server_id, update)
            config = configuration.mcp.get(saved.server_id)
            assert config is not None
            return saved, config

        saved, config = await asyncio.to_thread(save)
        try:
            await self._reconcile_mcp(config)
        except Exception:
            if previous is None:
                await asyncio.to_thread(configuration.remove_mcp_server, saved.server_id)
                raise
            await asyncio.to_thread(
                configuration.save_mcp_server,
                previous.server_id,
                MCPServerConfigInput.model_validate(
                    previous.model_dump(mode="python", exclude={"server_id", "metadata"})
                ),
            )
            restored = configuration.mcp.get(previous.server_id)
            assert restored is not None
            await self._reconcile_mcp(restored)
            raise
        return saved

    async def update_skill_discovery(
        self,
        *,
        enabled: bool | None = None,
        source_updates: Mapping[str, bool] | None = None,
    ) -> SkillPolicyView:
        configuration = self.configuration
        updates = dict(source_updates or {})
        previous = await asyncio.to_thread(configuration.skill_policy)
        if enabled is None and not updates:
            return previous
        saved = await asyncio.to_thread(
            configuration.update_skill_discovery,
            enabled=enabled,
            source_updates=updates,
        )
        try:
            await self._refresh_runtime_skills()
        except Exception:
            previous_sources = {source.source_id: source.enabled for source in previous.sources}
            await asyncio.to_thread(
                configuration.update_skill_discovery,
                enabled=previous.enabled if enabled is not None else None,
                source_updates={source_id: previous_sources[source_id] for source_id in updates},
            )
            raise
        return saved

    async def set_skill_enabled(
        self,
        name: str,
        *,
        enabled: bool,
    ) -> SkillManagementView:
        plugin_skill = next(
            (
                skill
                for skill in await self._available_skills()
                if skill.name == name and _plugin_skill_owner(skill) is not None
            ),
            None,
        )
        if plugin_skill is not None:
            raise ConfigError(f"plugin skill {name!r} is managed through its owning plugin")
        configuration = self.configuration
        result = await asyncio.to_thread(configuration.set_skill_enabled, name, enabled=enabled)
        try:
            await self._refresh_runtime_skills()
        except Exception:
            await asyncio.to_thread(
                configuration.set_skill_enabled,
                name,
                enabled=not enabled,
            )
            raise
        return result

    async def _refresh_runtime_skills(self) -> None:
        catalog = await asyncio.to_thread(self.configuration.skill_catalog)
        skills = tuple(
            SkillMetadata.model_validate(
                skill.model_dump(mode="python", exclude={"managed", "install"})
            )
            for skill in catalog.skills
        )
        await self._refresh_skills(skills)


def _plugin_skill_owner(skill: SkillMetadata) -> str | None:
    ouro = skill.extensions.get("ouro")
    if not isinstance(ouro, dict):
        return None
    plugin_id = ouro.get("plugin_id")
    return plugin_id if isinstance(plugin_id, str) and plugin_id else None
