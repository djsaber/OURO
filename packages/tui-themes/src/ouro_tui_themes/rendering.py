"""Frame-owned color runs, adapted to Textual only at the display boundary.

Animation producers publish single-cell glyph runs and packed RGB integers (or a
shared palette Style). Rendering and cuts keep that representation; Rich objects
are materialized only for CSS effects, filters, selection or export that need them.
No mutable frame buffer is borrowed from the numerical worker.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import lru_cache

from rich.color import Color as RichColor
from rich.color import ColorSystem
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual.cache import FIFOCache
from textual.color import Color
from textual.filter import ANSIToTruecolor, LineFilter
from textual.geometry import Region
from textual.strip import Strip
from textual.widget import Widget

type RunStyle = Style | int | None

_CACHE_SIZES = {
    "_divide_cache": 4,
    "_crop_cache": 16,
    "_style_cache": 16,
    "_filter_cache": 4,
    "_line_length_cache": 4,
    "_crop_extend_cache": 4,
    "_offsets_cache": 4,
}


def _resolved_rgb(style: RunStyle) -> bool:
    return (
        style is None
        or isinstance(style, int)
        or (
            not style.dim
            and (style.color is None or style.color.triplet is not None)
            and (style.bgcolor is None or style.bgcolor.triplet is not None)
        )
    )


def _rich_style(style: RunStyle) -> Style | None:
    if isinstance(style, int):
        return Style(color=RichColor.from_rgb(style >> 16, (style >> 8) & 255, style & 255))
    return style


@lru_cache(maxsize=32)
def _rgb_affixes(base: Style | None) -> tuple[str, str]:
    """Compile stable canvas attributes/background once, not once per RGB run."""
    attributes = base.without_color._make_ansi_codes(ColorSystem.TRUECOLOR) if base else ""
    background = ";".join(base.bgcolor.get_ansi_codes(False)) if base and base.bgcolor else ""
    return "\x1b[" + (attributes + ";" if attributes else ""), (
        ";" + background if background else ""
    ) + "m"


class FrameStrip(Strip):
    """An immutable row snapshot with lazy Rich/Textual compatibility state."""

    __slots__ = (
        "__weakref__",
        "_texts",
        "_run_styles",
        "_controls",
        "_single_cell",
        "_base_style",
        "_styled_segments",
    )

    _styled_segments: list[Segment] | None

    def __init__(self, segments: Iterable[Segment], cell_length: int | None = None) -> None:
        self._initialize(cell_length, None)
        self._segments = list(segments)

    @classmethod
    def from_runs(cls, texts: list[str], styles: list[RunStyle], cell_length: int) -> FrameStrip:
        """Take ownership of equal-length lists of single-cell glyph runs.

        Callers must not mutate either list after publishing the row. RGB stays
        packed in an int; palette Styles may be shared between frames.
        """
        row = cls.__new__(cls)
        row._texts, row._run_styles = texts, styles
        row._controls = None
        row._single_cell = True
        row._initialize(cell_length, None)
        return row

    def _initialize(self, cell_length: int | None, base_style: Style | None) -> None:
        self._base_style = base_style
        self._styled_segments = None
        self._cell_length = cell_length
        self._render_cache = None
        self._link_ids = None
        self._cell_count = None

    @property
    def _segments(self) -> list[Segment]:
        if self._styled_segments is None:
            segments = [
                Segment(text, _rich_style(style), self._controls[i] if self._controls else None)
                for i, (text, style) in enumerate(zip(self._texts, self._run_styles, strict=True))
            ]
            self._styled_segments = (
                list(Segment.apply_style(segments, self._base_style))
                if self._base_style
                else segments
            )
        return self._styled_segments

    @_segments.setter
    def _segments(self, segments: list[Segment]) -> None:
        self._texts = [segment.text for segment in segments]
        self._run_styles: list[RunStyle] = [segment.style for segment in segments]
        self._controls = (
            [segment.control for segment in segments] if any(s.control for s in segments) else None
        )
        self._single_cell = False
        self._base_style = None
        self._styled_segments = segments

    def __getattr__(self, name: str) -> FIFOCache:
        # Less common inherited operations keep Textual's normal cache policy.
        if name not in _CACHE_SIZES:
            raise AttributeError(name)
        cache: FIFOCache = FIFOCache(_CACHE_SIZES[name])
        setattr(self, name, cache)
        return cache

    def apply_style(self, style: Style) -> Strip:
        if style.link or any(isinstance(s, Style) and s.link for s in self._run_styles):
            # Rich assigns link IDs on composition. Materialize once so encoding
            # and later mouse hit-testing observe the same IDs.
            return FrameStrip(Segment.apply_style(self._segments, style), self.cell_length)
        row = FrameStrip.__new__(FrameStrip)
        row._texts, row._run_styles = self._texts, self._run_styles
        row._controls, row._single_cell = self._controls, self._single_cell
        row._initialize(self.cell_length, style + self._base_style if self._base_style else style)
        return row

    def apply_filter(self, filter: LineFilter, background: Color) -> Strip:
        if (
            type(filter) is ANSIToTruecolor
            and self._controls is None
            and _resolved_rgb(self._base_style)
            and all(map(_resolved_rgb, self._run_styles))
        ):
            return self
        return FrameStrip(filter.apply(self._segments, background), self.cell_length)

    def divide(self, cuts: Iterable[int]) -> Sequence[Strip]:
        cuts = tuple(cut for cut in cuts if cut <= self.cell_length)
        if cuts == (self.cell_length,):
            return (self,)
        if not self._single_cell:
            start = 0
            parts = []
            for segments, end in zip(Segment.divide(self._segments, cuts), cuts, strict=False):
                parts.append(FrameStrip(segments, end - start))
                start = end
            return parts
        # Published animation glyphs are all one cell wide. Split the run lists
        # in one pass, without converting runs to Segment/Style/Color objects.
        parts = []
        index = offset = start = 0
        for end in cuts:
            texts: list[str] = []
            styles: list[RunStyle] = []
            remaining = end - start
            while remaining > 0 and index < len(self._texts):
                text = self._texts[index]
                take = min(remaining, len(text) - offset)
                texts.append(text[offset : offset + take])
                styles.append(self._run_styles[index])
                remaining -= take
                offset += take
                if offset == len(text):
                    index += 1
                    offset = 0
            row = FrameStrip.from_runs(texts, styles, end - start)
            row._base_style = self._base_style
            parts.append(row)
            start = end
        return parts

    def crop(self, start: int, end: int | None = None) -> Strip:
        start = max(0, start)
        end = self.cell_length if end is None else min(self.cell_length, end)
        if start == 0 and end == self.cell_length:
            return self
        if end <= start:
            return FrameStrip((), 0)
        return self.divide((start, end))[-1] if start else self.divide((end,))[0]

    def render(self, console: Console) -> str:
        if self._render_cache is None:
            color_system = console._color_system or ColorSystem.TRUECOLOR
            base = self._base_style
            rgb_output = color_system == ColorSystem.TRUECOLOR and not (base and base.link)
            prefix, suffix = _rgb_affixes(base) if rgb_output else ("", "")
            rendered = []
            for i, (text, style) in enumerate(zip(self._texts, self._run_styles, strict=True)):
                if isinstance(style, int) and rgb_output:
                    rendered.append(
                        f"{prefix}38;2;{style >> 16};{(style >> 8) & 255};{style & 255}"
                        f"{suffix}{text}\x1b[0m"
                    )
                    continue
                effective = _rich_style(style)
                if base:
                    effective = None if self._controls and self._controls[i] else base + effective
                # The style's own ANSI cache suffices. A global 16K cache would
                # retain transient blended colors beyond their frame lifetime.
                rendered.append(
                    text
                    if effective is None
                    else (
                        effective.render(text, color_system=color_system)
                        if text
                        else self.render_style(effective, text, color_system)
                    )
                )
            self._render_cache = "".join(rendered)
        return self._render_cache


def render_frame_lines(widget: Widget, rows: Sequence[Strip], crop: Region) -> list[Strip] | None:
    """Adapt plain canvases; delegate CSS effects to Textual's existing renderer."""
    styles = widget.styles
    if (
        styles.gutter
        or any(edge for edge, _ in styles.outline)
        or styles.tint.a
        or styles.text_opacity != 1.0
        or widget.opacity != 1.0
        or styles.has_rule("hatch")
        or widget.size != widget.content_size
        or (widget.auto_links and widget.hover_style._link_id)
    ):
        return None
    filters = widget.get_line_filters()
    background = widget.background_colors[1]
    width = widget.size.width
    style = widget.rich_style
    lines = []
    for y in crop.line_range:
        line = rows[y].apply_style(style) if 0 <= y < len(rows) else FrameStrip.blank(width, style)
        if line.cell_length != width:
            line = line.adjust_cell_length(width, style)
        for filter in filters:
            line = line.apply_filter(filter, background)
        lines.append(line.crop(crop.x, crop.right))
    return lines
