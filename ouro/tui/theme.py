"""Accent palettes and runtime theme lookup for the OURO TUI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class TuiTheme:
    """One restrained accent palette used by both CSS and Rich content."""

    theme_id: str
    label: str
    description: str
    accent: str
    accent_bright: str


_THEMES = (
    TuiTheme(
        theme_id="harbor",
        label="Harbor",
        description="Muted ocean blue",
        accent="#6083ae",
        accent_bright="#7194bf",
    ),
    TuiTheme(
        theme_id="sage",
        label="Sage",
        description="Quiet eucalyptus green",
        accent="#6f9284",
        accent_bright="#80a395",
    ),
    TuiTheme(
        theme_id="iris",
        label="Iris",
        description="Soft smoked violet",
        accent="#867da5",
        accent_bright="#978eb6",
    ),
    TuiTheme(
        theme_id="rosewood",
        label="Rosewood",
        description="Understated dusty rose",
        accent="#9b747b",
        accent_bright="#ac858c",
    ),
    TuiTheme(
        theme_id="sandstone",
        label="Sandstone",
        description="Warm muted bronze",
        accent="#968264",
        accent_bright="#a79375",
    ),
    TuiTheme(
        theme_id="fjord",
        label="Fjord",
        description="Deep misted teal",
        accent="#5f898d",
        accent_bright="#709a9e",
    ),
    TuiTheme(
        theme_id="slate",
        label="Slate",
        description="Cool architectural blue-gray",
        accent="#718398",
        accent_bright="#8294a9",
    ),
    TuiTheme(
        theme_id="plum",
        label="Plum",
        description="Reserved muted mauve",
        accent="#8c728d",
        accent_bright="#9d839e",
    ),
    TuiTheme(
        theme_id="terracotta",
        label="Terracotta",
        description="Soft mineral clay",
        accent="#9b705f",
        accent_bright="#ac816f",
    ),
    TuiTheme(
        theme_id="olive",
        label="Olive",
        description="Calm weathered green",
        accent="#858765",
        accent_bright="#969876",
    ),
)

TUI_THEMES: Mapping[str, TuiTheme] = {theme.theme_id: theme for theme in _THEMES}
DEFAULT_TUI_THEME = TUI_THEMES["harbor"]


def theme_for(node: object) -> TuiTheme:
    """Resolve the owning app's palette, with a safe pre-mount default."""

    try:
        theme = getattr(cast(Any, node).app, "tui_theme", None)
    except (AttributeError, RuntimeError):
        return DEFAULT_TUI_THEME
    return theme if isinstance(theme, TuiTheme) else DEFAULT_TUI_THEME


__all__ = ["DEFAULT_TUI_THEME", "TUI_THEMES", "TuiTheme", "theme_for"]
