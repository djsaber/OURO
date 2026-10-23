"""Unicode prose wrapping at the Rich presentation boundary."""

from __future__ import annotations

from itertools import accumulate

from rich.cells import cell_len, chop_cells
from rich.console import Console, ConsoleOptions, JustifyMethod, OverflowMethod
from rich.containers import Lines
from rich.measure import Measurement
from rich.text import Text
from unicode_segmentation_rs import line_break_units

_SINGLE_LINE_TRANSLATION = str.maketrans("\r\n\t", "   ")


def wrap_offsets(text: str, width: int) -> list[int]:
    """Return source offsets for a single, tab-expanded line.

    Unicode owns legal breaks; Rich owns cell widths and grapheme-safe folding
    of overlong units. No characters are inserted into the source.
    """
    if width <= 0 or cell_len(text) <= width:
        return []
    offsets: list[int] = []
    position = cells = 0
    for unit in line_break_units(text):
        unit_width = cell_len(unit.rstrip())
        if cells and cells + unit_width > width:
            offsets.append(position)
            cells = 0
        if unit_width > width:
            parts = [part for part in chop_cells(unit, width) if part]
            offsets.extend(position + offset for offset in accumulate(map(len, parts[:-1])))
            cells = cell_len(parts[-1])
        else:
            cells += cell_len(unit)
        position += len(unit)
    return offsets


class ProseText(Text):
    """Rich Text with Unicode line boundaries and unchanged span semantics."""

    @classmethod
    def from_text(cls, text: Text) -> ProseText:
        return cls(
            text.plain,
            style=text.style,
            justify=text.justify,
            overflow=text.overflow,
            no_wrap=text.no_wrap,
            end=text.end,
            tab_size=text.tab_size,
            spans=text.spans.copy(),
        )

    def copy(self) -> ProseText:
        return self.from_text(self)

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        measurement = super().__rich_measure__(console, options)
        if self.plain.isascii() or self.no_wrap:
            return measurement
        minimum = max(
            (
                cell_len(unit.rstrip())
                for line in self.plain.splitlines()
                for unit in line_break_units(line)
            ),
            default=0,
        )
        return Measurement(minimum, measurement.maximum)

    def wrap(
        self,
        console: Console,
        width: int,
        *,
        justify: JustifyMethod | None = None,
        overflow: OverflowMethod | None = None,
        tab_size: int = 8,
        no_wrap: bool | None = None,
    ) -> Lines:
        if (
            self.plain.isascii()
            or (self.no_wrap if no_wrap is None else no_wrap)
            or (overflow or self.overflow or "fold") != "fold"
        ):
            return super().wrap(
                console,
                width,
                justify=justify,
                overflow=overflow,
                tab_size=tab_size,
                no_wrap=no_wrap,
            )
        lines = Lines()
        for line in self.split(allow_blank=True):
            line.expand_tabs(tab_size)
            # Rich still handles span slicing, alignment, padding and clipping.
            for section in line.divide(wrap_offsets(line.plain, width)):
                section.rstrip_end(width)
                lines.extend(section.wrap(console, width, justify=justify, no_wrap=True))
        return lines
