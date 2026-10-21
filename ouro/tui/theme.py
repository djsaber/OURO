"""Immutable visual themes and runtime theme lookup for the OURO TUI."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from colorsys import hls_to_rgb, rgb_to_hls
from dataclasses import dataclass, replace
from functools import lru_cache
from math import isfinite
from types import MappingProxyType
from typing import Any, ClassVar, cast

from pygments.token import Token  # type: ignore[import-untyped]
from rich.cells import cell_len
from rich.style import Style
from rich.syntax import SyntaxTheme, TokenType
from rich.text import Text
from rich.theme import Theme as RichTheme

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_MIN_SPINNER_INTERVAL_SECONDS = 0.05
_MAX_SPINNER_INTERVAL_SECONDS = 2.0
THEME_SATURATION_LEVELS = 32
DEFAULT_THEME_SATURATION_MIN = 0.2
DEFAULT_THEME_SATURATION_MAX = 1.0


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

    # Brand and interactive accents.
    accent: str
    accent_bright: str

    # Surfaces, from the page background to active inputs.
    background: str = "#0c0c0c"
    panel: str = "#171717"
    highlight: str = "#242424"
    input_highlight: str = "#2a2a2a"

    # Structural boundaries and control tracks.
    border_subtle: str = "#545454"
    border: str = "#585858"
    border_focus: str = "#464646"
    progress_track: str = "#373737"
    scrollbar_hover: str = "#737373"

    # Component-independent text prominence, from lowest to highest.
    text_disabled: str = "#626262"
    text_lowlight: str = "#676767"
    text_primary: str = "#ababab"
    text_emphasis: str = "#d9d9d9"
    text_highlight: str = "#e7e7e7"

    # Fixed semantic hues: progress, positive outcome, attention, risk, failure.
    running: str = "#6fa8a0"
    success: str = "#86ad7a"
    warning: str = "#b8a06a"
    danger: str = "#c18d6c"
    failure: str = "#c17b7b"

    # Syntax roles keep their own palette, independent of the brand accent.
    syntax_comment: str = "#707070"
    syntax_keyword: str = "#b8b8b8"
    syntax_string: str = "#77b083"
    syntax_number: str = "#968264"
    syntax_operator: str = "#999999"
    syntax_name: str = "#ababab"
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
    progress_filled_marker: str = "█"
    progress_empty_marker: str = "▒"
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
    saturation: float = DEFAULT_THEME_SATURATION_MAX
    saturation_min: float = DEFAULT_THEME_SATURATION_MIN
    saturation_max: float = DEFAULT_THEME_SATURATION_MAX

    _TEXT_COLOR_FIELDS: ClassVar[tuple[str, ...]] = (
        "text_disabled",
        "text_lowlight",
        "text_primary",
        "text_emphasis",
        "text_highlight",
    )

    # Palette colors follow the user's saturation preference; semantic colors
    # preserve the meaning and prominence of states and destructive actions.
    _PALETTE_COLOR_FIELDS: ClassVar[tuple[str, ...]] = (
        "accent",
        "accent_bright",
        "background",
        "panel",
        "highlight",
        "input_highlight",
        "border_subtle",
        "border",
        "border_focus",
        "progress_track",
        "scrollbar_hover",
        *_TEXT_COLOR_FIELDS,
        "syntax_comment",
        "syntax_keyword",
        "syntax_string",
        "syntax_number",
        "syntax_operator",
        "syntax_name",
    )
    _SEMANTIC_COLOR_FIELDS: ClassVar[tuple[str, ...]] = (
        "running",
        "success",
        "warning",
        "danger",
        "failure",
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
        "progress_filled_marker",
        "progress_empty_marker",
    )

    def __post_init__(self) -> None:
        for name in ("theme_id", "label", "description"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        for name in (*self._PALETTE_COLOR_FIELDS, *self._SEMANTIC_COLOR_FIELDS):
            value = getattr(self, name)
            if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
                raise ValueError(f"{name} must be a #RRGGBB color")
            object.__setattr__(self, name, value.lower())
        for name in self._GLYPH_FIELDS:
            self._validate_glyph(name, getattr(self, name))
        for name in ("saturation", "saturation_min", "saturation_max"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} must be between 0.0 and 1.0")
            object.__setattr__(self, name, float(value))
        if self.saturation_min > self.saturation_max:
            raise ValueError("saturation_min must not exceed saturation_max")
        if not self.saturation_min <= self.saturation <= self.saturation_max:
            raise ValueError("saturation must be within the theme saturation bounds")
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

    def saturation_for_level(self, level: int) -> float:
        """Map a high-to-low UI level onto this palette's declared bounds."""

        if isinstance(level, bool) or not isinstance(level, int):
            raise ValueError("saturation level must be an integer")
        if not 0 <= level < THEME_SATURATION_LEVELS:
            raise ValueError(
                f"saturation level must be between 0 and {THEME_SATURATION_LEVELS - 1}"
            )
        if level == 0:
            return self.saturation_max
        if level == THEME_SATURATION_LEVELS - 1:
            return self.saturation_min
        ratio = level / (THEME_SATURATION_LEVELS - 1)
        return self.saturation_max + (self.saturation_min - self.saturation_max) * ratio

    def saturation_level(self) -> int:
        """Return the nearest UI level for the theme's configured saturation."""

        span = self.saturation_max - self.saturation_min
        if span == 0:
            return 0
        ratio = (self.saturation_max - self.saturation) / span
        return round(ratio * (THEME_SATURATION_LEVELS - 1))

    def at_saturation(self, saturation: float) -> TuiTheme:
        """Adjust palette saturation while preserving semantic colors."""

        if (
            isinstance(saturation, bool)
            or not isinstance(saturation, (int, float))
            or not isfinite(saturation)
            or not self.saturation_min <= saturation <= self.saturation_max
        ):
            raise ValueError("saturation must be within the theme saturation bounds")
        return _theme_at_saturation(self, float(saturation))

    def at_saturation_level(self, level: int) -> TuiTheme:
        """Resolve the palette at one of the shared high-to-low UI levels."""

        return self.at_saturation(self.saturation_for_level(level))

    def accent_saturation_scale(self) -> tuple[str, ...]:
        """Return the dynamically derived 32-step accent preview scale."""

        return _accent_saturation_scale(self)

    def css_variables(self) -> dict[str, str]:
        """Project theme colors into Textual's CSS token namespace."""

        return {
            "ouro-theme": self.accent,
            "ouro-background": self.background,
            "ouro-panel": self.panel,
            "ouro-highlight": self.highlight,
            "ouro-input-highlight": self.input_highlight,
            "ouro-border-subtle": self.border_subtle,
            "ouro-border": self.border,
            "ouro-border-focus": self.border_focus,
            "ouro-progress-track": self.progress_track,
            "ouro-scrollbar-hover": self.scrollbar_hover,
            "ouro-text-disabled": self.text_disabled,
            "ouro-text-lowlight": self.text_lowlight,
            "ouro-text-primary": self.text_primary,
            "ouro-text-emphasis": self.text_emphasis,
            "ouro-text-highlight": self.text_highlight,
            "ouro-running": self.running,
            "ouro-success": self.success,
            "ouro-warning": self.warning,
            "ouro-danger": self.danger,
            "ouro-failure": self.failure,
        }

    def rich_theme(self) -> RichTheme:
        """Return the bounded cached Rich style projection for this value."""

        return _rich_theme(self)

    def highlight_text(self, text: Text) -> Text:
        """Highlight interactive text while retaining fixed semantic foregrounds."""

        semantic_colors = {
            Style(color=getattr(self, name)).color for name in self._SEMANTIC_COLOR_FIELDS
        }
        preserved = []
        for span in text.spans:
            style = Style.parse(span.style) if isinstance(span.style, str) else span.style
            if style.color in semantic_colors:
                preserved.append(span)
        highlighted = text.copy()
        highlighted.stylize(self.text_highlight)
        for span in preserved:
            highlighted.stylize(span.style, span.start, span.end)
        return highlighted

    def syntax_theme(self) -> SyntaxTheme:
        """Return the bounded cached Pygments projection for this value."""

        return _syntax_theme(self)


@lru_cache(maxsize=32)
def _rich_theme(theme: TuiTheme) -> RichTheme:
    return RichTheme(
        {
            "markdown.paragraph": Style(color=theme.text_emphasis),
            "markdown.text": Style(color=theme.text_emphasis),
            "markdown.em": Style(italic=True),
            "markdown.emph": Style(italic=True),
            "markdown.strong": Style(color=theme.text_emphasis, bold=True),
            "markdown.code": Style(color=theme.accent),
            "markdown.code_block": Style(color=theme.syntax_name, bgcolor=theme.panel),
            "markdown.block_quote": Style(color=theme.text_lowlight),
            "markdown.list": Style(color=theme.text_lowlight),
            "markdown.item": Style(color=theme.text_emphasis),
            "markdown.item.bullet": Style(color=theme.text_lowlight),
            "markdown.item.number": Style(color=theme.text_lowlight),
            "markdown.hr": Style(color=theme.border_subtle, dim=True),
            "markdown.h1.border": Style(color=theme.border_subtle),
            "markdown.h1": Style(color=theme.text_emphasis, bold=True),
            "markdown.h2": Style(color=theme.text_emphasis, bold=True),
            "markdown.h3": Style(color=theme.text_emphasis, bold=True),
            "markdown.h4": Style(color=theme.text_emphasis, bold=True),
            "markdown.h5": Style(color=theme.text_emphasis, bold=True),
            "markdown.h6": Style(color=theme.text_emphasis, bold=True),
            "markdown.h7": Style(color=theme.text_emphasis, bold=True),
            "markdown.link": Style(color=theme.accent, underline=True),
            "markdown.link_url": Style(color=theme.accent, underline=True),
            "markdown.s": Style(strike=True),
            "markdown.table.border": Style(color=theme.border_subtle),
            "markdown.table.header": Style(color=theme.text_emphasis, bold=True),
            "markdown.kbd": Style(color=theme.accent_bright, bold=True),
        },
        inherit=True,
    )


@lru_cache(maxsize=32)
def _syntax_theme(theme: TuiTheme) -> SyntaxTheme:
    return _TuiSyntaxTheme(theme)


def _color_at_saturation(color: str, saturation: float) -> str:
    red = int(color[1:3], 16) / 255
    green = int(color[3:5], 16) / 255
    blue = int(color[5:7], 16) / 255
    hue, lightness, source_saturation = rgb_to_hls(red, green, blue)
    resolved = hls_to_rgb(hue, lightness, min(1.0, source_saturation * saturation))
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in resolved)


@lru_cache(maxsize=256)
def _theme_at_saturation(theme: TuiTheme, saturation: float) -> TuiTheme:
    if saturation == theme.saturation_max and theme.saturation == theme.saturation_max:
        return theme
    colors = {
        name: _color_at_saturation(getattr(theme, name), saturation)
        for name in theme._PALETTE_COLOR_FIELDS
    }
    return replace(theme, saturation=saturation, **cast(Any, colors))


@lru_cache(maxsize=64)
def _accent_saturation_scale(theme: TuiTheme) -> tuple[str, ...]:
    return tuple(
        _color_at_saturation(theme.accent, theme.saturation_for_level(level))
        for level in range(THEME_SATURATION_LEVELS)
    )


# Keep the neutral default first, then follow the perceptual hue wheel through red.
_THEMES = (
    TuiTheme(
        theme_id="original",
        label="Original",
        description="Neutral original white",
        accent="#e8e8e8",
        accent_bright="#ffffff",
        text_disabled="#626262",
        text_lowlight="#676767",
        text_primary="#ababab",
        text_emphasis="#d9d9d9",
        text_highlight="#e7e7e7",
    ),
    TuiTheme(
        theme_id="blossom",
        label="Blossom",
        description="Refined rose",
        accent="#e08795",
        accent_bright="#f0a1ad",
        text_disabled="#665e5f",
        text_lowlight="#6b6364",
        text_primary="#aea8a9",
        text_emphasis="#dad8d8",
        text_highlight="#e8e6e6",
    ),
    TuiTheme(
        theme_id="coral",
        label="Coral",
        description="Soft warm coral",
        accent="#e18881",
        accent_bright="#f2a39c",
        text_disabled="#665f5e",
        text_lowlight="#6b6463",
        text_primary="#aea8a8",
        text_emphasis="#dad8d8",
        text_highlight="#e8e6e6",
    ),
    TuiTheme(
        theme_id="sienna",
        label="Sienna",
        description="Polished warm clay",
        accent="#de8e69",
        accent_bright="#eea788",
        text_disabled="#66615e",
        text_lowlight="#6b6663",
        text_primary="#aeaaa8",
        text_emphasis="#dad8d8",
        text_highlight="#e8e6e6",
    ),
    TuiTheme(
        theme_id="honey",
        label="Honey",
        description="Muted golden amber",
        accent="#cd9a50",
        accent_bright="#dfb274",
        text_disabled="#65635f",
        text_lowlight="#6b6863",
        text_primary="#aeaca8",
        text_emphasis="#dad9d8",
        text_highlight="#e8e7e6",
    ),
    TuiTheme(
        theme_id="citrine",
        label="Citrine",
        description="Balanced yellow olive",
        accent="#b5a64f",
        accent_bright="#c9bc73",
        text_disabled="#65645f",
        text_lowlight="#6a6964",
        text_primary="#adaca9",
        text_emphasis="#dadad8",
        text_highlight="#e8e8e6",
    ),
    TuiTheme(
        theme_id="fern",
        label="Fern",
        description="Fresh botanical green",
        accent="#86b46d",
        accent_bright="#a0c98b",
        text_disabled="#616460",
        text_lowlight="#666965",
        text_primary="#aaada9",
        text_emphasis="#d9dad8",
        text_highlight="#e7e8e6",
    ),
    TuiTheme(
        theme_id="jade",
        label="Jade",
        description="Calm mint green",
        accent="#60ba8d",
        accent_bright="#82cea6",
        text_disabled="#5f6562",
        text_lowlight="#646a67",
        text_primary="#a9adab",
        text_emphasis="#d8dad9",
        text_highlight="#e6e8e7",
    ),
    TuiTheme(
        theme_id="lagoon",
        label="Lagoon",
        description="Clear coastal teal",
        accent="#39bab4",
        accent_bright="#6acec8",
        text_disabled="#5f6565",
        text_lowlight="#636b6a",
        text_primary="#a8aeae",
        text_emphasis="#d8dada",
        text_highlight="#e6e8e8",
    ),
    TuiTheme(
        theme_id="glacier",
        label="Glacier",
        description="Cool mineral cyan",
        accent="#3db6cf",
        accent_bright="#6bcae1",
        text_disabled="#5e6566",
        text_lowlight="#636a6b",
        text_primary="#a8adae",
        text_emphasis="#d8dada",
        text_highlight="#e6e8e8",
    ),
    TuiTheme(
        theme_id="azure",
        label="Azure",
        description="Open sky blue",
        accent="#57afe0",
        accent_bright="#7bc5ef",
        text_disabled="#5e6366",
        text_lowlight="#63686b",
        text_primary="#a8acae",
        text_emphasis="#d8dada",
        text_highlight="#e6e7e8",
    ),
    TuiTheme(
        theme_id="cobalt",
        label="Cobalt",
        description="Polished medium blue",
        accent="#74a7e8",
        accent_bright="#91bef7",
        text_disabled="#5e6166",
        text_lowlight="#63666b",
        text_primary="#a8aaae",
        text_emphasis="#d8d9da",
        text_highlight="#e6e7e8",
    ),
    TuiTheme(
        theme_id="indigo",
        label="Indigo",
        description="Soft blue violet",
        accent="#909fe9",
        accent_bright="#a8b6f8",
        text_disabled="#5e5f66",
        text_lowlight="#63646b",
        text_primary="#a8a9ae",
        text_emphasis="#d8d8da",
        text_highlight="#e6e6e8",
    ),
    TuiTheme(
        theme_id="orchid",
        label="Orchid",
        description="Refined violet",
        accent="#a996e2",
        accent_bright="#bfaff2",
        text_disabled="#605e66",
        text_lowlight="#65636b",
        text_primary="#aaa8ae",
        text_emphasis="#d8d8da",
        text_highlight="#e6e6e8",
    ),
    TuiTheme(
        theme_id="wisteria",
        label="Wisteria",
        description="Gentle purple",
        accent="#bf8fd4",
        accent_bright="#d2a8e5",
        text_disabled="#635f65",
        text_lowlight="#68646a",
        text_primary="#aca8ae",
        text_emphasis="#dad8da",
        text_highlight="#e7e6e8",
    ),
    TuiTheme(
        theme_id="peony",
        label="Peony",
        description="Modern magenta rose",
        accent="#d389ba",
        accent_bright="#e4a3ce",
        text_disabled="#655f63",
        text_lowlight="#6a6468",
        text_primary="#aea8ac",
        text_emphasis="#dad8da",
        text_highlight="#e8e6e7",
    ),
    TuiTheme(
        theme_id="lilac",
        label="Lilac",
        description="Quiet pink mauve",
        accent="#db87a8",
        accent_bright="#eca1be",
        text_disabled="#655f61",
        text_lowlight="#6b6366",
        text_primary="#aea8aa",
        text_emphasis="#dad8d9",
        text_highlight="#e8e6e7",
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


def label_text(value: str, *, theme: TuiTheme, style: str = "") -> Text:
    """Style generated UI labels, keeping separators independent of their fields.

    Literal messages, code, and editable values keep their own rendering path.
    """

    text = Text(value, style=style)
    text.highlight_words(
        (theme.inline_separator, theme.section_separator),
        Style(color=theme.text_lowlight, bold=False, italic=False, dim=False),
    )
    return text


__all__ = [
    "DEFAULT_THEME_SATURATION_MAX",
    "DEFAULT_THEME_SATURATION_MIN",
    "DEFAULT_TUI_THEME",
    "THEME_SATURATION_LEVELS",
    "TUI_THEMES",
    "TuiTheme",
    "label_text",
    "theme_for",
]
