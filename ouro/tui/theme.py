"""Immutable visual themes and runtime theme lookup for the OURO TUI."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from math import isfinite
from types import MappingProxyType
from typing import Any, ClassVar, cast

from pygments.token import Token  # type: ignore[import-untyped]
from rich.cells import cell_len
from rich.style import Style
from rich.syntax import SyntaxTheme, TokenType
from rich.theme import Theme as RichTheme

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_MIN_SPINNER_INTERVAL_SECONDS = 0.05
_MAX_SPINNER_INTERVAL_SECONDS = 2.0


class _TuiSyntaxTheme(SyntaxTheme):
    """A compact Pygments projection backed only by immutable theme colors."""

    def __init__(self, theme: TuiTheme) -> None:
        def token_style(color: str) -> Style:
            return Style(color=color, bgcolor=theme.panel)

        self._styles: Mapping[TokenType, Style] = {
            Token: token_style(theme.syntax_name),
            Token.Text: token_style(theme.syntax_name),
            Token.Name: token_style(theme.syntax_name),
            Token.Name.Builtin: token_style(theme.syntax_keyword),
            Token.Comment: token_style(theme.syntax_comment),
            Token.Keyword: token_style(theme.syntax_keyword),
            Token.Literal.String: token_style(theme.syntax_string),
            Token.Literal.Number: token_style(theme.syntax_number),
            Token.Operator: token_style(theme.syntax_operator),
            Token.Punctuation: token_style(theme.syntax_operator),
            Token.Error: token_style(theme.syntax_error),
        }
        self._background = Style(bgcolor=theme.panel)
        self._cache: dict[TokenType, Style] = {}

    def get_style_for_token(self, token_type: TokenType) -> Style:
        try:
            return self._cache[token_type]
        except KeyError:
            token = token_type
            while token not in self._styles and token:
                token = token[:-1]
            style = self._styles.get(token, self._styles[Token])
            self._cache[token_type] = style
            return style

    def get_background_style(self) -> Style:
        return self._background


@dataclass(frozen=True, slots=True)
class TuiTheme:
    """One complete, declarative visual theme for an OURO TUI App."""

    theme_id: str
    label: str
    description: str
    accent: str
    accent_bright: str

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
    running: str = "#77b083"
    success: str = "#77b083"

    syntax_comment: str = "#647180"
    syntax_keyword: str = "#7aa2c7"
    syntax_string: str = "#77b083"
    syntax_number: str = "#968264"
    syntax_operator: str = "#8f9aa8"
    syntax_name: str = "#d6d9df"
    syntax_error: str = "#d87575"

    section_separator_mark: str = "•"
    inline_separator_mark: str = "•"
    brand_mark: str = "▣"
    mask_character: str = "•"
    selected_choice_marker: str = "●"
    unselected_choice_marker: str = "○"
    active_item_marker: str = "◉"
    completed_item_marker: str = "●"
    pending_item_marker: str = "○"
    session_node_mark: str = "■"
    selected_session_node_mark: str = "▣"
    expanded_disclosure_marker: str = "-"
    collapsed_disclosure_marker: str = "+"
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
    spinner_interval_seconds: float = 0.12

    _COLOR_FIELDS: ClassVar[tuple[str, ...]] = (
        "accent",
        "accent_bright",
        "background",
        "panel",
        "highlight",
        "input_highlight",
        "progress_track",
        "border_focus",
        "separator_tone",
        "border",
        "text_disabled",
        "text_faint",
        "scrollbar_hover",
        "text_lowlight",
        "text_metadata",
        "text_detail",
        "text_choice",
        "text_muted",
        "warning",
        "text_secondary",
        "danger",
        "text_primary",
        "failure",
        "critical",
        "text_highlight",
        "warning_bright",
        "running",
        "success",
        "syntax_comment",
        "syntax_keyword",
        "syntax_string",
        "syntax_number",
        "syntax_operator",
        "syntax_name",
        "syntax_error",
    )
    _GLYPH_FIELDS: ClassVar[tuple[str, ...]] = (
        "section_separator_mark",
        "inline_separator_mark",
        "brand_mark",
        "mask_character",
        "selected_choice_marker",
        "unselected_choice_marker",
        "active_item_marker",
        "completed_item_marker",
        "pending_item_marker",
        "session_node_mark",
        "selected_session_node_mark",
        "expanded_disclosure_marker",
        "collapsed_disclosure_marker",
    )

    def __post_init__(self) -> None:
        for name in ("theme_id", "label", "description"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        for name in self._COLOR_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
                raise ValueError(f"{name} must be a #RRGGBB color")
            object.__setattr__(self, name, value.lower())
        for name in self._GLYPH_FIELDS:
            self._validate_glyph(name, getattr(self, name))
        if self.selected_choice_marker == self.unselected_choice_marker:
            raise ValueError("selected and unselected choice markers must differ")
        if self.expanded_disclosure_marker == self.collapsed_disclosure_marker:
            raise ValueError("expanded and collapsed disclosure markers must differ")
        if not isinstance(self.spinner_frames, tuple):
            raise ValueError("spinner_frames must be a tuple")
        if not self.spinner_frames:
            raise ValueError("spinner_frames must not be empty")
        if len(set(self.spinner_frames)) != len(self.spinner_frames):
            raise ValueError("spinner_frames must be unique")
        for frame in self.spinner_frames:
            self._validate_glyph("spinner frame", frame)
        interval = self.spinner_interval_seconds
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not isfinite(interval)
            or not _MIN_SPINNER_INTERVAL_SECONDS <= interval <= _MAX_SPINNER_INTERVAL_SECONDS
        ):
            raise ValueError(
                "spinner_interval_seconds must be between "
                f"{_MIN_SPINNER_INTERVAL_SECONDS} and "
                f"{_MAX_SPINNER_INTERVAL_SECONDS}"
            )
        object.__setattr__(self, "spinner_interval_seconds", float(interval))

    @staticmethod
    def _validate_glyph(name: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must not be empty")
        if "\x1b" in value or any(
            unicodedata.category(character).startswith("C") for character in value
        ):
            raise ValueError(f"{name} must not contain control characters")
        if cell_len(value) != 1:
            raise ValueError(f"{name} must occupy exactly one terminal cell")

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
        """Project theme colors into Textual's CSS token namespace."""

        return {
            "ouro-theme": self.accent,
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
            "ouro-text-metadata": self.text_metadata,
            "ouro-text-detail": self.text_detail,
            "ouro-text-choice": self.text_choice,
            "ouro-text-muted": self.text_muted,
            "ouro-text-secondary": self.text_secondary,
            "ouro-text-primary": self.text_primary,
            "ouro-text-highlight": self.text_highlight,
            "ouro-running": self.running,
            "ouro-success": self.success,
            "ouro-warning": self.warning,
            "ouro-warning-bright": self.warning_bright,
            "ouro-danger": self.danger,
            "ouro-failure": self.failure,
            "ouro-critical": self.critical,
        }

    def rich_theme(self) -> RichTheme:
        """Return the bounded cached Rich style projection for this value."""

        return _rich_theme(self)

    def syntax_theme(self) -> SyntaxTheme:
        """Return the bounded cached Pygments projection for this value."""

        return _syntax_theme(self)


@lru_cache(maxsize=32)
def _rich_theme(theme: TuiTheme) -> RichTheme:
    return RichTheme(
        {
            "markdown.paragraph": Style(color=theme.text_primary),
            "markdown.text": Style(color=theme.text_primary),
            "markdown.em": Style(italic=True),
            "markdown.emph": Style(italic=True),
            "markdown.strong": Style(bold=True),
            "markdown.code": Style(color=theme.accent),
            "markdown.code_block": Style(color=theme.syntax_name, bgcolor=theme.panel),
            "markdown.block_quote": Style(color=theme.text_muted),
            "markdown.list": Style(color=theme.text_muted),
            "markdown.item": Style(color=theme.text_primary),
            "markdown.item.bullet": Style(color=theme.text_muted),
            "markdown.item.number": Style(color=theme.text_muted),
            "markdown.hr": Style(color=theme.separator_tone, dim=True),
            "markdown.h1.border": Style(color=theme.separator_tone),
            "markdown.h1": Style(color=theme.text_primary, bold=True),
            "markdown.h2": Style(color=theme.text_primary, bold=True),
            "markdown.h3": Style(color=theme.text_primary, bold=True),
            "markdown.h4": Style(color=theme.text_secondary, bold=True),
            "markdown.h5": Style(color=theme.text_secondary, bold=True),
            "markdown.h6": Style(color=theme.text_secondary, bold=True),
            "markdown.h7": Style(color=theme.text_secondary, dim=True),
            "markdown.link": Style(color=theme.accent, underline=True),
            "markdown.link_url": Style(color=theme.accent, underline=True),
            "markdown.s": Style(strike=True),
            "markdown.table.border": Style(color=theme.separator_tone),
            "markdown.table.header": Style(color=theme.text_primary, bold=True),
            "markdown.kbd": Style(color=theme.accent_bright, bold=True),
        },
        inherit=True,
    )


@lru_cache(maxsize=32)
def _syntax_theme(theme: TuiTheme) -> SyntaxTheme:
    return _TuiSyntaxTheme(theme)


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
    TuiTheme(
        theme_id="azure",
        label="Azure",
        description="Bright sky blue",
        accent="#6cb6e8",
        accent_bright="#8ac8f0",
    ),
    TuiTheme(
        theme_id="jade",
        label="Jade",
        description="Bright mint green",
        accent="#5fc19c",
        accent_bright="#7fd2b0",
    ),
    TuiTheme(
        theme_id="orchid",
        label="Orchid",
        description="Bright soft violet",
        accent="#b09ae8",
        accent_bright="#c3b1f2",
    ),
    TuiTheme(
        theme_id="coral",
        label="Coral",
        description="Bright warm coral",
        accent="#f0947e",
        accent_bright="#f7ab97",
    ),
    TuiTheme(
        theme_id="honey",
        label="Honey",
        description="Bright warm amber",
        accent="#d9ab63",
        accent_bright="#e7c07e",
    ),
    TuiTheme(
        theme_id="blossom",
        label="Blossom",
        description="Bright soft pink",
        accent="#e59ab3",
        accent_bright="#f0b0c6",
    ),
    TuiTheme(
        theme_id="cobalt",
        label="Cobalt",
        description="Rich steel blue",
        accent="#7aa4d3",
        accent_bright="#98b9dd",
    ),
    TuiTheme(
        theme_id="lagoon",
        label="Lagoon",
        description="Rich cool teal",
        accent="#48b1a8",
        accent_bright="#6fc4bd",
    ),
    TuiTheme(
        theme_id="sienna",
        label="Sienna",
        description="Rich warm clay",
        accent="#cd9569",
        accent_bright="#d9ad8b",
    ),
    TuiTheme(
        theme_id="lilac",
        label="Lilac",
        description="Rich dusty mauve",
        accent="#c990aa",
        accent_bright="#d5a9be",
    ),
    TuiTheme(
        theme_id="fern",
        label="Fern",
        description="Bright leaf green",
        accent="#74bc65",
        accent_bright="#87ce78",
    ),
    TuiTheme(
        theme_id="indigo",
        label="Indigo",
        description="Bright cool indigo",
        accent="#9ca6f7",
        accent_bright="#b1b9ff",
    ),
    TuiTheme(
        theme_id="wisteria",
        label="Wisteria",
        description="Bright soft purple",
        accent="#ce93ec",
        accent_bright="#e0a5fe",
    ),
    TuiTheme(
        theme_id="peony",
        label="Peony",
        description="Bright magenta pink",
        accent="#ea8ed3",
        accent_bright="#fca0e5",
    ),
)

TUI_THEMES: Mapping[str, TuiTheme] = MappingProxyType({theme.theme_id: theme for theme in _THEMES})
DEFAULT_TUI_THEME = TUI_THEMES["original"]


def theme_for(node: object) -> TuiTheme:
    """Resolve the owning App's theme, with a safe pre-mount default."""

    initial = getattr(node, "_initial_tui_theme", None)
    try:
        theme = getattr(cast(Any, node).app, "tui_theme", None)
    except AttributeError, RuntimeError:
        return initial if isinstance(initial, TuiTheme) else DEFAULT_TUI_THEME
    if isinstance(theme, TuiTheme):
        return theme
    return initial if isinstance(initial, TuiTheme) else DEFAULT_TUI_THEME


__all__ = [
    "DEFAULT_TUI_THEME",
    "TUI_THEMES",
    "TuiTheme",
    "theme_for",
]
