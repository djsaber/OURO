from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from ouro.core.errors import ConfigError
from ouro.tui.preferences import (
    MAX_RECENT_WORKSPACES,
    TuiPreferences,
    TuiPreferencesStore,
)
from ouro.tui.theme import (
    DEFAULT_TUI_THEME,
    THEME_SATURATION_LEVELS,
)


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


def test_tui_preferences_round_trip_in_frontend_owned_file(tmp_path: Path) -> None:
    store = TuiPreferencesStore(tmp_path)

    assert TuiPreferences().session_grouping == "date"
    assert TuiPreferences().reasoning_expanded is False
    assert TuiPreferences().show_scaffold_messages is False
    assert TuiPreferences().input_draft_memory_enabled is True
    assert TuiPreferences().theme_saturation_level == 0
    store.save(
        TuiPreferences(
            theme="indigo",
            session_grouping="workspace",
            theme_saturation_level=17,
            reasoning_expanded=False,
            show_scaffold_messages=True,
            input_draft_memory_enabled=False,
            recent_actions=("/new", "__ouro_theme__"),
            recent_workspaces=(str(tmp_path),),
        )
    )

    assert store.path == tmp_path / "config" / "tui.json"
    assert store.load() == TuiPreferences(
        theme="indigo",
        session_grouping="workspace",
        theme_saturation_level=17,
        reasoning_expanded=False,
        show_scaffold_messages=True,
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
        {"session_grouping": "namespace"},
        {"session_grouping": None},
        {"theme": "neon"},
        {"theme": "cobalt", "theme_saturation_level": -1},
        {"theme": "cobalt", "theme_saturation_level": THEME_SATURATION_LEVELS},
        {"theme": "cobalt", "theme_saturation_level": True},
        {"theme": "cobalt", "reasoning_expanded": "sometimes"},
        {"show_scaffold_messages": "sometimes"},
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
