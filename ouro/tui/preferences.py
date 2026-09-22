"""Durable presentation preferences owned exclusively by the TUI."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError, field_validator

from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.local_io import read_text_atomic, write_model_atomic
from ouro.tui.theme import DEFAULT_TUI_THEME, THEME_SATURATION_LEVELS, TUI_THEMES

MAX_RECENT_WORKSPACES = 20

# The levels approximate each retired palette when an older file has no explicit level.
_LEGACY_THEME_MIGRATIONS: dict[str, tuple[str, int]] = {
    "harbor": ("cobalt", 21),
    "sage": ("jade", 25),
    "iris": ("orchid", 26),
    "rosewood": ("blossom", 28),
    "sandstone": ("honey", 25),
    "fjord": ("lagoon", 25),
    "slate": ("azure", 30),
    "plum": ("wisteria", 30),
    "terracotta": ("sienna", 24),
    "olive": ("citrine", 25),
}


class TuiPreferences(OuroModel):
    theme: str = DEFAULT_TUI_THEME.theme_id
    theme_saturation_level: int = 0
    reasoning_expanded: bool = False
    input_draft_memory_enabled: bool = True
    recent_actions: tuple[str, ...] = ()
    recent_workspaces: tuple[str, ...] = ()

    @field_validator("theme")
    @classmethod
    def validate_theme(cls, value: str) -> str:
        if value not in TUI_THEMES:
            available = ", ".join(TUI_THEMES)
            raise ValueError(f"unknown TUI theme {value!r}; expected one of: {available}")
        return value

    @field_validator("theme_saturation_level", mode="before")
    @classmethod
    def validate_theme_saturation_level(cls, value: object) -> int:
        if type(value) is not int or not 0 <= value < THEME_SATURATION_LEVELS:
            raise ValueError(
                f"theme_saturation_level must be between 0 and {THEME_SATURATION_LEVELS - 1}"
            )
        return value

    @field_validator("recent_actions")
    @classmethod
    def validate_recent_actions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 3:
            raise ValueError("recent_actions must contain at most three entries")
        if any(not action.strip() for action in value):
            raise ValueError("recent_actions entries must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("recent_actions entries must be unique")
        return value

    @field_validator("recent_workspaces")
    @classmethod
    def validate_recent_workspaces(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_RECENT_WORKSPACES:
            raise ValueError(
                f"recent_workspaces must contain at most {MAX_RECENT_WORKSPACES} entries"
            )
        if any(not workspace.strip() for workspace in value):
            raise ValueError("recent_workspaces entries must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("recent_workspaces entries must be unique")
        return value


class TuiPreferencesStore:
    """Read and atomically replace the TUI-only preference artifact."""

    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        ouro_home = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.path = ouro_home / "config" / "tui.json"

    def load(self) -> TuiPreferences:
        if not self.path.exists():
            return TuiPreferences()
        try:
            raw = json.loads(read_text_atomic(self.path))
            legacy_theme = raw.get("theme") if isinstance(raw, dict) else None
            migration = (
                _LEGACY_THEME_MIGRATIONS.get(legacy_theme)
                if isinstance(legacy_theme, str)
                else None
            )
            if migration is not None:
                theme, saturation_level = migration
                raw = {**raw, "theme": theme}
                raw.setdefault("theme_saturation_level", saturation_level)
            return TuiPreferences.model_validate(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid TUI preferences {self.path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read TUI preferences {self.path}: {exc}") from exc
        except ValidationError as exc:
            raise ConfigError(f"invalid TUI preferences {self.path}: {exc}") from exc

    def save(self, preferences: TuiPreferences) -> None:
        write_model_atomic(self.path, preferences)


__all__ = ["MAX_RECENT_WORKSPACES", "TuiPreferences", "TuiPreferencesStore"]
