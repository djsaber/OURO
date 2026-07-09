from __future__ import annotations

import json
import os
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from ouro.core.errors import ConfigError
from ouro.prompts import DEFAULT_SYSTEM_PROMPT


@dataclass(frozen=True)
class PromptConfigSnapshot:
    system_prompt: str | None
    config_path: Path

    @property
    def has_system_prompt(self) -> bool:
        return self.system_prompt is not None


class PromptConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.config_path = self.root / "config" / "config.json"

    def snapshot(self) -> PromptConfigSnapshot:
        raw = self.load_raw()
        return PromptConfigSnapshot(
            system_prompt=self._system_prompt_from_raw(raw),
            config_path=self.config_path,
        )

    def resolve_system_prompt(self, override: str | None = None) -> str:
        if override is not None:
            return override
        snapshot = self.snapshot()
        if snapshot.system_prompt is not None:
            return snapshot.system_prompt
        return DEFAULT_SYSTEM_PROMPT

    def set_system_prompt(self, prompt: str) -> PromptConfigSnapshot:
        raw = self.load_raw()
        next_raw = deepcopy(raw)
        prompts = next_raw.get("prompts")
        if not isinstance(prompts, dict):
            prompts = {}
        prompts["system"] = prompt
        next_raw["prompts"] = prompts
        self._write_raw(next_raw)
        return self.snapshot()

    def clear_system_prompt(self) -> PromptConfigSnapshot:
        raw = self.load_raw()
        next_raw = deepcopy(raw)
        prompts = next_raw.get("prompts")
        if isinstance(prompts, dict):
            prompts.pop("system", None)
            if prompts:
                next_raw["prompts"] = prompts
            else:
                next_raw.pop("prompts", None)
        next_raw.pop("system_prompt", None)
        self._write_raw(next_raw)
        return self.snapshot()

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

    def _system_prompt_from_raw(self, raw: Mapping[str, Any]) -> str | None:
        prompts = raw.get("prompts")
        if isinstance(prompts, Mapping):
            system_prompt = prompts.get("system")
            if isinstance(system_prompt, str):
                return system_prompt
            if system_prompt is not None:
                raise ConfigError("prompts.system must be a string")
        elif prompts is not None:
            raise ConfigError("prompts config must be an object")

        legacy_system_prompt = raw.get("system_prompt")
        if isinstance(legacy_system_prompt, str):
            return legacy_system_prompt
        if legacy_system_prompt is not None:
            raise ConfigError("system_prompt must be a string")
        return None

    def _write_raw(self, raw: Mapping[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.config_path.parent,
            delete=False,
        ) as temp:
            temp.write(text)
            temp.write("\n")
            temp_name = temp.name
        Path(temp_name).replace(self.config_path)


def prompt_config_to_public_dict(snapshot: PromptConfigSnapshot) -> dict[str, Any]:
    effective = (
        snapshot.system_prompt if snapshot.system_prompt is not None else DEFAULT_SYSTEM_PROMPT
    )
    return {
        "configured": snapshot.has_system_prompt,
        "system_prompt": snapshot.system_prompt,
        "effective_system_prompt": effective,
    }
