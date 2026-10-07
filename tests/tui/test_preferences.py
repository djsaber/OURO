from __future__ import annotations

import json
import re
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from pygments.token import Token

from ouro.core.errors import ConfigError
from ouro.tui.preferences import (
    MAX_RECENT_WORKSPACES,
    TuiPreferences,
    TuiPreferencesStore,
)
from ouro.tui.theme import (
    DEFAULT_THEME_SATURATION_MAX,
    DEFAULT_THEME_SATURATION_MIN,
    DEFAULT_TUI_THEME,
    THEME_SATURATION_LEVELS,
    TUI_THEMES,
    TuiTheme,
)


def test_tui_theme_catalog_uses_distinct_restrained_palettes() -> None:
    assert tuple(TUI_THEMES) == (
        "original",
        "blossom",
        "coral",
        "sienna",
        "honey",
        "citrine",
        "fern",
        "jade",
        "lagoon",
        "glacier",
        "azure",
        "cobalt",
        "indigo",
        "orchid",
        "wisteria",
        "peony",
        "lilac",
    )
    assert DEFAULT_TUI_THEME.theme_id in TUI_THEMES
    assert len({theme.accent for theme in TUI_THEMES.values()}) == len(TUI_THEMES)
    assert all(theme.accent.startswith("#") for theme in TUI_THEMES.values())
    assert all(theme.accent_bright.startswith("#") for theme in TUI_THEMES.values())
    assert {(theme.saturation_min, theme.saturation_max) for theme in TUI_THEMES.values()} == {
        (DEFAULT_THEME_SATURATION_MIN, DEFAULT_THEME_SATURATION_MAX)
    }
    lagoon_scale = TUI_THEMES["lagoon"].accent_saturation_scale()
    assert len(lagoon_scale) == THEME_SATURATION_LEVELS
    assert lagoon_scale[0] == TUI_THEMES["lagoon"].accent
    assert lagoon_scale[-1] != lagoon_scale[0]


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
    muted = theme.at_saturation_level(THEME_SATURATION_LEVELS - 1)
    assert muted.saturation == theme.saturation_min
    assert muted.accent == theme.accent_saturation_scale()[-1]
    assert muted.accent != theme.accent
    assert muted.panel != theme.panel
    assert muted.warning == theme.warning
    with pytest.raises(FrozenInstanceError):
        theme.accent = "#ffffff"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"theme_id": ""}, "theme_id"),
        ({"accent": "red"}, "accent"),
        ({"danger": "red"}, "danger"),
        ({"syntax_error": "red"}, "syntax_error"),
        ({"brand_mark": "XX"}, "brand_mark"),
        ({"brand_mark": "\n"}, "control characters"),
        ({"unselected_choice_marker": "●"}, "must differ"),
        ({"spinner_frames": ()}, "must not be empty"),
        ({"spinner_frames": ("◐", "◐")}, "must be unique"),
        ({"spinner_frames": ["◐"]}, "must be a tuple"),
        ({"spinner_interval_seconds": 0.01}, "between"),
        ({"spinner_interval_seconds": float("inf")}, "between"),
        ({"spinner_interval_seconds": True}, "between"),
        ({"saturation_min": -0.1}, "saturation_min"),
        ({"saturation_max": 1.1}, "saturation_max"),
        ({"saturation_min": 0.8, "saturation_max": 0.7}, "must not exceed"),
        ({"saturation": 0.1}, "within the theme saturation bounds"),
    ),
)
def test_tui_theme_rejects_invalid_token_values(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(DEFAULT_TUI_THEME, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("theme", TUI_THEMES.values(), ids=TUI_THEMES)
def test_saturation_preserves_semantic_colors_and_render_projections(theme: TuiTheme) -> None:
    semantic_fields = (
        "warning",
        "warning_bright",
        "danger",
        "failure",
        "critical",
        "running",
        "success",
    )
    for level in range(THEME_SATURATION_LEVELS):
        resolved = theme.at_saturation_level(level)
        for field in semantic_fields:
            assert getattr(resolved, field) == getattr(theme, field)
            variable = f"ouro-{field.replace('_', '-')}"
            assert resolved.css_variables()[variable] == theme.css_variables()[variable]
        assert resolved.syntax_error == theme.syntax_error
        assert resolved.syntax_theme().get_style_for_token(Token.Error) == (
            theme.syntax_theme().get_style_for_token(Token.Error)
        )
    assert theme.at_saturation_level(0) is theme


def test_custom_semantic_colors_survive_saturation_changes() -> None:
    theme = replace(DEFAULT_TUI_THEME, danger="#FF1234", syntax_error="#ED2345")
    muted = theme.at_saturation(theme.saturation_min)
    assert muted.danger == "#ff1234"
    assert muted.syntax_error == "#ed2345"
    assert muted.at_saturation(theme.saturation_max).danger == theme.danger


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
    assert TuiPreferences().theme_saturation_level == 0
    store.save(
        TuiPreferences(
            theme="indigo",
            theme_saturation_level=17,
            reasoning_expanded=False,
            input_draft_memory_enabled=False,
            recent_actions=("/new", "__ouro_theme__"),
            recent_workspaces=(str(tmp_path),),
        )
    )

    assert store.path == tmp_path / "config" / "tui.json"
    assert store.load() == TuiPreferences(
        theme="indigo",
        theme_saturation_level=17,
        reasoning_expanded=False,
        input_draft_memory_enabled=False,
        recent_actions=("/new", "__ouro_theme__"),
        recent_workspaces=(str(tmp_path),),
    )

    store.path.write_text(json.dumps({"theme": "harbor"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown TUI theme"):
        store.load()


@pytest.mark.parametrize(
    "raw",
    (
        {"theme": "neon"},
        {"theme": "cobalt", "theme_saturation_level": -1},
        {"theme": "cobalt", "theme_saturation_level": THEME_SATURATION_LEVELS},
        {"theme": "cobalt", "theme_saturation_level": True},
        {"theme": "cobalt", "reasoning_expanded": "sometimes"},
        {"theme": "cobalt", "input_draft_memory_enabled": "sometimes"},
        {"theme": "cobalt", "recent_actions": ["one", "two", "three", "four"]},
        {"theme": "cobalt", "recent_actions": ["same", "same"]},
        {
            "theme": "cobalt",
            "recent_workspaces": [
                f"workspace-{index}" for index in range(MAX_RECENT_WORKSPACES + 1)
            ],
        },
        {"theme": "cobalt", "recent_workspaces": ["same", "same"]},
        {"theme": "cobalt", "recent_workspaces": ["  "]},
    ),
)
def test_tui_preferences_reject_invalid_values(tmp_path: Path, raw: dict[str, object]) -> None:
    store = TuiPreferencesStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid TUI preferences"):
        store.load()
