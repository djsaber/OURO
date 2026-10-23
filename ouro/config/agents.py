from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.config.providers.manager import ProviderConfigManager
from ouro.core.agents import AgentDefinition, AgentSpawnPolicy
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.core.model_selection import DEFAULT_MODEL

MAX_USER_AGENT_DEFINITIONS = 32
RESERVED_AGENT_IDS = frozenset({"general"})


class AgentRuntimeConfig(OuroModel):
    """Process-start policy for first-release subagent dispatch."""

    max_depth: Literal[1] = 1
    spawn_policy: AgentSpawnPolicy = Field(default_factory=AgentSpawnPolicy)

    @model_validator(mode="after")
    def validate_first_release_policy(self) -> Self:
        if self.spawn_policy.allowed_lifecycle_bindings != ("bound",):
            raise ValueError("first-release Agent runtime supports only bound lifecycle")
        return self


class AgentDefinitionsConfig(OuroModel):
    revision: int = Field(default=0, ge=0)
    runtime: AgentRuntimeConfig = Field(default_factory=AgentRuntimeConfig)
    definitions: tuple[AgentDefinition, ...] = Field(
        default=(),
        max_length=MAX_USER_AGENT_DEFINITIONS,
    )

    @model_validator(mode="after")
    def validate_definitions(self) -> Self:
        ids = [item.agent_id for item in self.definitions]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate agent_id")
        reserved = sorted(RESERVED_AGENT_IDS.intersection(ids))
        if reserved:
            raise ValueError("reserved agent ids cannot be user-defined: " + ", ".join(reserved))
        return self


class AgentDefinitionsConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root, name="agents.json")

    @property
    def file(self) -> JsonConfigFile:
        return self._file

    def snapshot(self) -> AgentDefinitionsConfig:
        raw = self._file.read().get("agents", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ConfigError("agents config must be an object")
        try:
            config = AgentDefinitionsConfig.model_validate(dict(raw))
        except ValidationError as exc:
            raise ConfigError(f"invalid agents config: {exc}") from exc
        return config

    @atomic_config_update
    def create(
        self,
        definition: AgentDefinition,
        *,
        expected_revision: int,
    ) -> AgentDefinitionsConfig:
        if definition.agent_id in RESERVED_AGENT_IDS:
            raise ConfigError(f"agent_id is reserved: {definition.agent_id}")
        if definition.model == DEFAULT_MODEL:
            ProviderConfigManager(self._file.root).require_default_route()
        current = self._require_revision(expected_revision)
        if any(item.agent_id == definition.agent_id for item in current.definitions):
            raise ConfigError(f"agent definition already exists: {definition.agent_id}")
        return self._save(
            current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "definitions": (*current.definitions, definition),
                }
            )
        )

    @atomic_config_update
    def update(
        self,
        definition: AgentDefinition,
        *,
        expected_revision: int,
    ) -> AgentDefinitionsConfig:
        if definition.agent_id in RESERVED_AGENT_IDS:
            raise ConfigError(f"builtin agent definition is immutable: {definition.agent_id}")
        if definition.model == DEFAULT_MODEL:
            ProviderConfigManager(self._file.root).require_default_route()
        current = self._require_revision(expected_revision)
        if not any(item.agent_id == definition.agent_id for item in current.definitions):
            raise ConfigError(f"agent definition not found: {definition.agent_id}")
        return self._save(
            AgentDefinitionsConfig(
                revision=current.revision + 1,
                runtime=current.runtime,
                definitions=tuple(
                    definition if item.agent_id == definition.agent_id else item
                    for item in current.definitions
                ),
            )
        )

    @atomic_config_update
    def delete(self, agent_id: str, *, expected_revision: int) -> AgentDefinitionsConfig:
        if agent_id in RESERVED_AGENT_IDS:
            raise ConfigError(f"builtin agent definition is immutable: {agent_id}")
        current = self._require_revision(expected_revision)
        if not any(item.agent_id == agent_id for item in current.definitions):
            raise ConfigError(f"agent definition not found: {agent_id}")
        return self._save(
            AgentDefinitionsConfig(
                revision=current.revision + 1,
                runtime=current.runtime,
                definitions=tuple(
                    item for item in current.definitions if item.agent_id != agent_id
                ),
            )
        )

    @atomic_config_update
    def update_runtime(
        self,
        runtime: AgentRuntimeConfig,
        *,
        expected_revision: int,
    ) -> AgentDefinitionsConfig:
        current = self._require_revision(expected_revision)
        try:
            validated = AgentRuntimeConfig.model_validate(runtime.model_dump())
        except ValueError as exc:
            raise ConfigError(f"invalid Agent runtime config: {exc}") from exc
        return self._save(
            AgentDefinitionsConfig(
                revision=current.revision + 1,
                runtime=validated,
                definitions=current.definitions,
            )
        )

    def _require_revision(self, expected_revision: int) -> AgentDefinitionsConfig:
        current = self.snapshot()
        if current.revision != expected_revision:
            raise ConfigError(
                f"agents revision conflict: expected {expected_revision}, found {current.revision}"
            )
        return current

    def _save(self, config: AgentDefinitionsConfig) -> AgentDefinitionsConfig:
        try:
            validated = AgentDefinitionsConfig.model_validate(config.model_dump())
        except ValidationError as exc:
            raise ConfigError(f"invalid agents config: {exc}") from exc
        raw = deepcopy(self._file.read())
        raw["agents"] = validated.model_dump(mode="json")
        self._file.write(raw)
        return validated


__all__ = [
    "AgentDefinitionsConfig",
    "AgentDefinitionsConfigManager",
    "AgentRuntimeConfig",
    "MAX_USER_AGENT_DEFINITIONS",
    "RESERVED_AGENT_IDS",
]
