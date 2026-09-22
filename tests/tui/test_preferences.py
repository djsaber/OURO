from __future__ import annotations

import json
import re
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from ouro.core.errors import ConfigError
from ouro.tui.preferences import (
    MAX_RECENT_WORKSPACES,
    TuiPreferences,
    TuiPreferencesStore,
)
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_THEMES, TuiTheme


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
        "azure",
        "jade",
        "orchid",
        "coral",
        "honey",
        "blossom",
        "cobalt",
        "lagoon",
        "sienna",
        "lilac",
        "fern",
        "indigo",
        "wisteria",
        "peony",
    )
    assert DEFAULT_TUI_THEME.theme_id in TUI_THEMES
    assert len({theme.accent for theme in TUI_THEMES.values()}) == len(TUI_THEMES)
    assert all(theme.accent.startswith("#") for theme in TUI_THEMES.values())
    assert all(theme.accent_bright.startswith("#") for theme in TUI_THEMES.values())


def test_tui_theme_owns_shared_symbols_colors_and_animation() -> None:
    assert DEFAULT_TUI_THEME.section_separator == " • "
    assert DEFAULT_TUI_THEME.inline_separator == " • "
    assert DEFAULT_TUI_THEME.brand_mark == "▣"
    assert DEFAULT_TUI_THEME.selected_choice_marker == "●"
    assert DEFAULT_TUI_THEME.expanded_disclosure_marker == "-"
    assert DEFAULT_TUI_THEME.collapsed_disclosure_marker == "+"
    assert len(DEFAULT_TUI_THEME.spinner_frames) == 10
    assert len(set(DEFAULT_TUI_THEME.spinner_frames)) == len(DEFAULT_TUI_THEME.spinner_frames)
    assert all(value.startswith("#") for value in DEFAULT_TUI_THEME.css_variables().values())


def test_custom_tui_theme_is_complete_immutable_and_caches_render_projections() -> None:
    theme = replace(
        DEFAULT_TUI_THEME,
        theme_id="graphite",
        label="Graphite",
        description="High contrast test theme",
        accent="#67B7D1",
        panel="#102030",
        brand_mark="◆",
        selected_choice_marker="■",
        spinner_frames=("◐", "◓", "◑", "◒"),
        spinner_interval_seconds=0.2,
    )

    assert theme.accent == "#67b7d1"
    assert theme.css_variables()["ouro-panel"] == "#102030"
    assert theme.rich_theme() is theme.rich_theme()
    assert theme.syntax_theme() is theme.syntax_theme()
    with pytest.raises(FrozenInstanceError):
        theme.accent = "#ffffff"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"theme_id": ""}, "theme_id"),
        ({"accent": "red"}, "accent"),
        ({"brand_mark": "XX"}, "brand_mark"),
        ({"brand_mark": "\n"}, "control characters"),
        ({"unselected_choice_marker": "●"}, "must differ"),
        ({"spinner_frames": ()}, "must not be empty"),
        ({"spinner_frames": ("◐", "◐")}, "must be unique"),
        ({"spinner_frames": ["◐"]}, "must be a tuple"),
        ({"spinner_interval_seconds": 0.01}, "between"),
        ({"spinner_interval_seconds": float("inf")}, "between"),
        ({"spinner_interval_seconds": True}, "between"),
    ),
)
def test_tui_theme_rejects_invalid_token_values(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(DEFAULT_TUI_THEME, **changes)  # type: ignore[arg-type]


def test_tui_theme_type_exposes_the_complete_builtin_contract() -> None:
    assert all(isinstance(theme, TuiTheme) for theme in TUI_THEMES.values())
    assert all(theme.spinner_interval_seconds >= 0.05 for theme in TUI_THEMES.values())
    with pytest.raises(TypeError):
        TUI_THEMES["custom"] = DEFAULT_TUI_THEME  # type: ignore[index]


def test_tui_components_do_not_hardcode_shared_design_tokens() -> None:
    tui_root = Path(__file__).parents[2] / "ouro" / "tui"
    forbidden_symbols = {
        DEFAULT_TUI_THEME.section_separator_mark,
        DEFAULT_TUI_THEME.inline_separator_mark,
        DEFAULT_TUI_THEME.brand_mark,
        DEFAULT_TUI_THEME.selected_choice_marker,
        *DEFAULT_TUI_THEME.spinner_frames,
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
    assert TuiPreferences().input_draft_memory_enabled is True
    store.save(
        TuiPreferences(
            theme="iris",
            reasoning_expanded=False,
            input_draft_memory_enabled=False,
            recent_actions=("/new", "__ouro_theme__"),
            recent_workspaces=(str(tmp_path),),
        )
    )

    assert store.path == tmp_path / "config" / "tui.json"
    assert store.load() == TuiPreferences(
        theme="iris",
        reasoning_expanded=False,
        input_draft_memory_enabled=False,
        recent_actions=("/new", "__ouro_theme__"),
        recent_workspaces=(str(tmp_path),),
    )


@pytest.mark.parametrize(
    "raw",
    (
        {"theme": "neon"},
        {"theme": "harbor", "reasoning_expanded": "sometimes"},
        {"theme": "harbor", "input_draft_memory_enabled": "sometimes"},
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
def test_tui_preferences_reject_invalid_values(tmp_path: Path, raw: dict[str, object]) -> None:
    store = TuiPreferencesStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid TUI preferences"):
        store.load()
