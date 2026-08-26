"""Accent palettes and runtime theme lookup for the OURO TUI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class TuiDesignTokens:
    """Theme-independent visual vocabulary shared by CSS and Rich content."""

    section_separator_mark: str = "•"
    inline_separator_mark: str = "·"
    brand_mark: str = "▣"
    mask_character: str = "•"
    selected_choice_marker: str = "●"
    unselected_choice_marker: str = "○"
    active_item_marker: str = "◉"
    completed_item_marker: str = "●"
    pending_item_marker: str = "○"
    spinner_frames: tuple[str, ...] = (
        "⠋",
        "⠙",
        "⠹",
        "⠸",
        "⠼",
        "⠴",
        "⠦",
        "⠧",
        "⠇",
        "⠏",
    )
    background: str = "#0c0c0c"
    panel: str = "#171717"
    highlight: str = "#242424"
    input_highlight: str = "#2a2a2a"
    progress_track: str = "#34373d"
    border_focus: str = "#3f4650"
    separator_tone: str = "#4d5560"
    border: str = "#505967"
    text_disabled: str = "#626b78"
    text_faint: str = "#647180"
    scrollbar_hover: str = "#6a7482"
    text_lowlight: str = "#6f7a87"
    text_metadata: str = "#777f8b"
    text_detail: str = "#788492"
    text_choice: str = "#7d8794"
    text_muted: str = "#8f9aa8"
    warning: str = "#968264"
    text_secondary: str = "#aeb5bf"
    danger: str = "#b97878"
    text_primary: str = "#d6d9df"
    failure: str = "#d87575"
    critical: str = "#e08a8a"
    text_highlight: str = "#e5e7eb"
    warning_bright: str = "#facc15"
    success: str = "#77b083"

    @property
    def section_separator(self) -> str:
        return f" {self.section_separator_mark} "

    @property
    def inline_separator(self) -> str:
        return f" {self.inline_separator_mark} "

    @property
    def idle_mark(self) -> str:
        return self.inline_separator_mark

    def css_variables(self) -> dict[str, str]:
        """Project design colors into Textual's CSS token namespace."""

        return {
            "ouro-background": self.background,
            "ouro-panel": self.panel,
            "ouro-highlight": self.highlight,
            "ouro-input-highlight": self.input_highlight,
            "ouro-progress-track": self.progress_track,
            "ouro-border-focus": self.border_focus,
            "ouro-separator": self.separator_tone,
            "ouro-border": self.border,
            "ouro-text-disabled": self.text_disabled,
            "ouro-text-faint": self.text_faint,
            "ouro-scrollbar-hover": self.scrollbar_hover,
            "ouro-text-lowlight": self.text_lowlight,
            "ouro-text-detail": self.text_detail,
            "ouro-text-muted": self.text_muted,
            "ouro-text-secondary": self.text_secondary,
            "ouro-text-primary": self.text_primary,
            "ouro-text-highlight": self.text_highlight,
            "ouro-danger": self.danger,
            "ouro-warning-bright": self.warning_bright,
        }


TUI_DESIGN = TuiDesignTokens()


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
        theme_id="original",
        label="Original",
        description="Neutral original white",
        accent="#e8e8e8",
        accent_bright="#ffffff",
    ),
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
DEFAULT_TUI_THEME = TUI_THEMES["original"]


def theme_for(node: object) -> TuiTheme:
    """Resolve the owning app's palette, with a safe pre-mount default."""

    try:
        theme = getattr(cast(Any, node).app, "tui_theme", None)
    except (AttributeError, RuntimeError):
        return DEFAULT_TUI_THEME
    return theme if isinstance(theme, TuiTheme) else DEFAULT_TUI_THEME


__all__ = [
    "DEFAULT_TUI_THEME",
    "TUI_DESIGN",
    "TUI_THEMES",
    "TuiDesignTokens",
    "TuiTheme",
    "theme_for",
]
