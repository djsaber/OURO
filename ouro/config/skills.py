from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from ouro.config.file import JsonConfigFile
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError


class SkillPathConfig(OuroModel):
    path: str
    source: Literal["user", "workspace", "plugin"] = "user"


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
    scan_agents: bool = True
    scan_claude: bool = True
    scan_copilot: bool = True
    scan_codex: bool = True
    scan_github: bool = True
    scan_legacy: bool = True
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

    def record_install(self, name: str, record: ManagedSkillRecord) -> SkillConfigSnapshot:
        return self.record_installs({name: record})

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
