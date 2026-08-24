from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError

SkillDiscoverySourceId = Literal[
    "agents.user",
    "claude.user",
    "copilot.user",
    "codex.user",
    "ouro.user",
    "agents.workspace",
    "claude.workspace",
    "github.workspace",
    "codex.workspace",
    "ouro.workspace",
]

SKILL_DISCOVERY_SOURCE_IDS: tuple[SkillDiscoverySourceId, ...] = (
    "agents.user",
    "claude.user",
    "copilot.user",
    "codex.user",
    "ouro.user",
    "agents.workspace",
    "claude.workspace",
    "github.workspace",
    "codex.workspace",
    "ouro.workspace",
)
_SKILL_DISCOVERY_SOURCE_ID_SET = frozenset(SKILL_DISCOVERY_SOURCE_IDS)
_REMOVED_SCAN_FIELDS = frozenset(
    {
        "scan_agents",
        "scan_claude",
        "scan_copilot",
        "scan_codex",
        "scan_github",
    }
)


class SkillPathConfig(OuroModel):
    path: str
    source: Literal["user", "workspace"] = "user"
    enabled: bool = True


SkillInstallScope = Literal["user", "workspace"]


class ManagedSkillRecord(OuroModel):
    source: str
    scope: SkillInstallScope
    target: str
    selector: str | None = None
    ref: str | None = None


class SkillConfig(OuroModel):
    enabled: bool = True
    paths: list[SkillPathConfig] = Field(default_factory=list)
    disabled: set[str] = Field(default_factory=set)
    disabled_sources: set[SkillDiscoverySourceId] = Field(default_factory=set)
    max_depth: int = Field(default=6, ge=0, le=12)
    max_directories: int = Field(default=2000, ge=1, le=100_000)
    installed: dict[str, ManagedSkillRecord] = Field(default_factory=dict)


@dataclass(frozen=True)
class SkillConfigSnapshot:
    config: SkillConfig
    config_path: Path


class SkillConfigManager:
    """Read Ouro-specific skill policy from the shared ``config/config.json`` file."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root)
        self.root = self._file.root
        self.config_path = self._file.path

    def snapshot(self) -> SkillConfigSnapshot:
        raw = self.load_raw()
        section = raw.get("skills", {})
        if section is None:
            section = {}
        if not isinstance(section, Mapping):
            raise ConfigError("skills config must be an object")
        normalized = dict(section)
        removed_fields = sorted(_REMOVED_SCAN_FIELDS & normalized.keys())
        if removed_fields:
            fields = ", ".join(removed_fields)
            raise ConfigError(
                "skills config uses removed scan fields "
                f"({fields}); use disabled_sources with scoped source ids instead"
            )
        paths = normalized.get("paths")
        if isinstance(paths, list):
            normalized["paths"] = [
                {"path": item, "source": "user"} if isinstance(item, str) else item
                for item in paths
            ]
        try:
            config = SkillConfig.model_validate(normalized)
        except ValidationError as exc:
            raise ConfigError(f"invalid skills config: {exc}") from exc
        return SkillConfigSnapshot(config=config, config_path=self.config_path)

    def load_raw(self) -> dict[str, Any]:
        return self._file.read()

    @atomic_config_update
    def set_skill_enabled(self, name: str, *, enabled: bool) -> SkillConfigSnapshot:
        raw = self.load_raw()
        skills = self._skills_section(raw)
        disabled = {
            str(item) for item in skills.get("disabled", []) if isinstance(item, str) and item
        }
        if enabled:
            disabled.discard(name)
        else:
            disabled.add(name)
        skills["disabled"] = sorted(disabled)
        self._write_skills(raw, skills)
        return self.snapshot()

    @atomic_config_update
    def update_discovery(
        self,
        *,
        enabled: bool | None = None,
        source_updates: Mapping[str, bool] | None = None,
    ) -> SkillConfigSnapshot:
        """Atomically update Host directory discovery without touching plugin state."""

        current = self.snapshot().config
        updates = dict(source_updates or {})
        unknown = sorted(set(updates) - _SKILL_DISCOVERY_SOURCE_ID_SET)
        if unknown:
            raise ConfigError(f"unknown skill discovery source: {', '.join(unknown)}")

        disabled_sources: set[str] = set(current.disabled_sources)
        for source_id, source_enabled in updates.items():
            if source_enabled:
                disabled_sources.discard(source_id)
            else:
                disabled_sources.add(source_id)

        raw = self.load_raw()
        skills = self._skills_section(raw)
        if enabled is not None:
            skills["enabled"] = enabled
        if disabled_sources:
            skills["disabled_sources"] = sorted(disabled_sources)
        else:
            skills.pop("disabled_sources", None)
        self._write_skills(raw, skills)
        return self.snapshot()

    @atomic_config_update
    def record_install(self, name: str, record: ManagedSkillRecord) -> SkillConfigSnapshot:
        return self.record_installs({name: record})

    @atomic_config_update
    def record_installs(
        self,
        records: Mapping[str, ManagedSkillRecord],
    ) -> SkillConfigSnapshot:
        raw = self.load_raw()
        skills = self._skills_section(raw)
        installed = skills.get("installed")
        if not isinstance(installed, dict):
            installed = {}
        for name, record in records.items():
            installed[name] = record.model_dump(mode="json", exclude_none=True)
        skills["installed"] = installed
        disabled = {
            str(item) for item in skills.get("disabled", []) if isinstance(item, str) and item
        }
        disabled.difference_update(records)
        skills["disabled"] = sorted(disabled)
        self._write_skills(raw, skills)
        return self.snapshot()

    @atomic_config_update
    def forget_install(self, name: str) -> SkillConfigSnapshot:
        raw = self.load_raw()
        skills = self._skills_section(raw)
        installed = skills.get("installed")
        if isinstance(installed, dict):
            installed.pop(name, None)
            if installed:
                skills["installed"] = installed
            else:
                skills.pop("installed", None)
        self._write_skills(raw, skills)
        return self.snapshot()

    def _skills_section(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        section = raw.get("skills")
        if section is None:
            return {}
        if not isinstance(section, Mapping):
            raise ConfigError("skills config must be an object")
        return deepcopy(dict(section))

    def _write_skills(self, raw: Mapping[str, Any], skills: Mapping[str, Any]) -> None:
        next_raw = deepcopy(dict(raw))
        next_raw["skills"] = deepcopy(dict(skills))
        self._write_raw(next_raw)

    def _write_raw(self, raw: Mapping[str, Any]) -> None:
        self._file.write(raw)
