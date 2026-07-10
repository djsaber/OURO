from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.skills.models import SkillSource


class SkillPathConfig(OuroModel):
    path: str
    source: SkillSource = "user"


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


@dataclass(frozen=True)
class SkillConfigSnapshot:
    config: SkillConfig
    config_path: Path


class SkillConfigManager:
    """Read Ouro-specific skill policy from the shared ``config/config.json`` file."""

    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.config_path = self.root / "config" / "config.json"

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
        if not self.config_path.exists():
            return {}
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid config file {self.config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"invalid config file {self.config_path}: expected object")
        return raw
