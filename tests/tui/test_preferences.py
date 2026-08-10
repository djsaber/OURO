from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouro.core.errors import ConfigError
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_THEMES


def test_tui_theme_catalog_uses_distinct_restrained_palettes() -> None:
    assert tuple(TUI_THEMES) == (
        "original",
        "harbor",
        "sage",
        "iris",
        "rosewood",
        "sandstone",
        "fjord",
        "slate",
        "plum",
        "terracotta",
        "olive",
    )
    assert DEFAULT_TUI_THEME.theme_id in TUI_THEMES
    assert len({theme.accent for theme in TUI_THEMES.values()}) == len(TUI_THEMES)
    assert all(theme.accent.startswith("#") for theme in TUI_THEMES.values())
    assert all(theme.accent_bright.startswith("#") for theme in TUI_THEMES.values())


def test_tui_preferences_round_trip_in_frontend_owned_file(tmp_path: Path) -> None:
    store = TuiPreferencesStore(tmp_path)

    store.save(TuiPreferences(theme="iris"))

    assert store.path == tmp_path / "config" / "tui.json"
    assert store.load() == TuiPreferences(theme="iris")


@pytest.mark.parametrize(
    "raw",
    (
        {"theme": "neon"},
        {"theme": "harbor", "framework_option": True},
    ),
)
def test_tui_preferences_reject_unknown_values_and_fields(
    tmp_path: Path, raw: dict[str, object]
) -> None:
    store = TuiPreferencesStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid TUI preferences"):
        store.load()
