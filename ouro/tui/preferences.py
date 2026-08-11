"""Durable presentation preferences owned exclusively by the TUI."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError, field_validator

from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.local_io import read_text_atomic, write_model_atomic
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_THEMES


class TuiPreferences(OuroModel):
    theme: str = DEFAULT_TUI_THEME.theme_id
    reasoning_expanded: bool = False

    @field_validator("theme")
    @classmethod
    def validate_theme(cls, value: str) -> str:
        if value not in TUI_THEMES:
            available = ", ".join(TUI_THEMES)
            raise ValueError(f"unknown TUI theme {value!r}; expected one of: {available}")
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
            return TuiPreferences.model_validate(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid TUI preferences {self.path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read TUI preferences {self.path}: {exc}") from exc
        except ValidationError as exc:
            raise ConfigError(f"invalid TUI preferences {self.path}: {exc}") from exc

    def save(self, preferences: TuiPreferences) -> None:
        write_model_atomic(self.path, preferences)


__all__ = ["TuiPreferences", "TuiPreferencesStore"]
