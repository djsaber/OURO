from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ouro.config.file import JsonConfigFile, atomic_config_update
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
        self._file = JsonConfigFile(root)
        self.root = self._file.root
        self.config_path = self._file.path

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

    @atomic_config_update
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

    @atomic_config_update
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
        self._write_raw(next_raw)
        return self.snapshot()

    def load_raw(self) -> dict[str, Any]:
        return self._file.read()

    def _system_prompt_from_raw(self, raw: Mapping[str, Any]) -> str | None:
        if "system_prompt" in raw:
            raise ConfigError("unsupported root config field system_prompt; use prompts.system")
        prompts = raw.get("prompts")
        if isinstance(prompts, Mapping):
            system_prompt = prompts.get("system")
            if isinstance(system_prompt, str):
                return system_prompt
            if system_prompt is not None:
                raise ConfigError("prompts.system must be a string")
        elif prompts is not None:
            raise ConfigError("prompts config must be an object")

        return None

    def _write_raw(self, raw: Mapping[str, Any]) -> None:
        self._file.write(raw)


def prompt_config_to_public_dict(snapshot: PromptConfigSnapshot) -> dict[str, Any]:
    effective = (
        snapshot.system_prompt if snapshot.system_prompt is not None else DEFAULT_SYSTEM_PROMPT
    )
    return {
        "configured": snapshot.has_system_prompt,
        "system_prompt": snapshot.system_prompt,
        "effective_system_prompt": effective,
    }
