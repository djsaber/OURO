from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ouro.core.errors import ConfigError
from ouro.tui.preferences import (
    MAX_RECENT_WORKSPACES,
    TuiPreferences,
    TuiPreferencesStore,
)
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_DESIGN, TUI_THEMES


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


def test_tui_design_tokens_own_shared_symbols_colors_and_animation() -> None:
    assert TUI_DESIGN.section_separator == " • "
    assert TUI_DESIGN.inline_separator == " • "
    assert TUI_DESIGN.brand_mark == "▣"
    assert TUI_DESIGN.selected_choice_marker == "●"
    assert TUI_DESIGN.expanded_disclosure_marker == "-"
    assert TUI_DESIGN.collapsed_disclosure_marker == "+"
    assert len(TUI_DESIGN.spinner_frames) == 10
    assert len(set(TUI_DESIGN.spinner_frames)) == len(TUI_DESIGN.spinner_frames)
    assert all(value.startswith("#") for value in TUI_DESIGN.css_variables().values())


def test_tui_components_do_not_hardcode_shared_design_tokens() -> None:
    tui_root = Path(__file__).parents[2] / "ouro" / "tui"
    forbidden_symbols = {
        TUI_DESIGN.section_separator_mark,
        TUI_DESIGN.inline_separator_mark,
        TUI_DESIGN.brand_mark,
        TUI_DESIGN.selected_choice_marker,
        *TUI_DESIGN.spinner_frames,
    }
    violations: list[str] = []
    for path in tui_root.glob("*.py"):
        if path.name == "theme.py":
            continue
        source = path.read_text(encoding="utf-8")
        if any(symbol in source for symbol in forbidden_symbols) or re.search(
            r"#[0-9a-fA-F]{6}", source
        ):
            violations.append(path.name)

    assert violations == []


def test_tui_preferences_round_trip_in_frontend_owned_file(tmp_path: Path) -> None:
    store = TuiPreferencesStore(tmp_path)

    assert TuiPreferences().reasoning_expanded is False
    store.save(
        TuiPreferences(
            theme="iris",
            reasoning_expanded=False,
            recent_actions=("/new", "__ouro_theme__"),
            recent_workspaces=(str(tmp_path),),
        )
    )

    assert store.path == tmp_path / "config" / "tui.json"
    assert store.load() == TuiPreferences(
        theme="iris",
        reasoning_expanded=False,
        recent_actions=("/new", "__ouro_theme__"),
        recent_workspaces=(str(tmp_path),),
    )


@pytest.mark.parametrize(
    "raw",
    (
        {"theme": "neon"},
        {"theme": "harbor", "reasoning_expanded": "sometimes"},
        {"theme": "harbor", "recent_actions": ["one", "two", "three", "four"]},
        {"theme": "harbor", "recent_actions": ["same", "same"]},
        {
            "theme": "harbor",
            "recent_workspaces": [
                f"workspace-{index}" for index in range(MAX_RECENT_WORKSPACES + 1)
            ],
        },
        {"theme": "harbor", "recent_workspaces": ["same", "same"]},
        {"theme": "harbor", "recent_workspaces": ["  "]},
    ),
)
def test_tui_preferences_reject_invalid_values(
    tmp_path: Path, raw: dict[str, object]
) -> None:
    store = TuiPreferencesStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid TUI preferences"):
        store.load()
