"""Textual document geometry using the shared prose wrapping policy."""

from __future__ import annotations

from bisect import bisect_right
from itertools import accumulate

from rich.cells import cell_len
from textual._cells import cell_width_to_column_index
from textual._wrap import compute_wrap_offsets
from textual.document._document import Location
from textual.document._wrapped_document import WrappedDocument
from textual.expand_tabs import get_tab_widths
from textual.geometry import Offset

from ouro.tui.text_wrapping import wrap_offsets


class ProseDocument(WrappedDocument):
    """Keep Textual's document, editing and navigation; replace only wrapping.

    Textual 8 exposes no wrap-policy callback. Its geometry caches are owned
    here so edits reflow only changed source lines; subsequent rows only have
    their visual indices updated. No dependency globals are patched.
    """

    def wrap(self, width: int, tab_width: int | None = None) -> None:
        if all(line.isascii() for line in self.document.lines):
            super().wrap(width, tab_width)
            return
        self._width = width
        if tab_width is not None:
            self._tab_width = tab_width
        self._wrap_offsets = []
        self._tab_width_cache = []
        self._offset_to_line_info = []
        self._line_index_to_offsets = []
        self._replace_lines(0, 0, self.document.line_count)

    def wrap_range(self, start: Location, old_end: Location, new_end: Location) -> None:
        last_shared_line = min(len(self._wrap_offsets), self.document.line_count) - 1
        first = min(max(0, min(start[0], old_end[0])), last_shared_line)
        old_stop = min(max(first, start[0], old_end[0]) + 1, len(self._wrap_offsets))
        new_stop = min(max(first, new_end[0]) + 1, self.document.line_count)
        if all(self.document.get_line(index).isascii() for index in range(first, new_stop)):
            super().wrap_range(start, old_end, new_end)
            return
        self._replace_lines(first, old_stop, new_stop)

    def location_to_offset(self, location: Location) -> Offset:
        offset = super().location_to_offset(location)
        row, column = location
        if "\t" not in self.document.get_line(row):
            return offset
        section = bisect_right(self.get_offsets(row), column)
        start, expanded, ends = self._expanded_section(row, section)
        expanded_column = ends[column - start - 1] if column > start else 0
        return Offset(cell_len(expanded[:expanded_column]), offset.y)

    def get_target_document_column(self, line_index: int, x_offset: int, y_offset: int) -> int:
        if "\t" not in self.document.get_line(line_index):
            return super().get_target_document_column(line_index, x_offset, y_offset)
        start, expanded, ends = self._expanded_section(line_index, y_offset)
        column = bisect_right(
            ends,
            cell_width_to_column_index(expanded, x_offset, self._tab_width),
        )
        if y_offset not in (-1, len(self.get_offsets(line_index))):
            column = min(column, len(ends) - 1)
        return start + column

    def _expanded_section(self, row: int, section: int) -> tuple[int, str, list[int]]:
        source = self.document.get_line(row)
        starts = [0, *self.get_offsets(row), len(source)]
        if section < 0:
            section += len(starts) - 1
        start, end = starts[section : section + 2]
        tabs_before = source[:start].count("\t")
        expanded, ends = _expand_tabs(source[start:end], self.get_tab_widths(row)[tabs_before:])
        return start, expanded, ends

    def _replace_lines(self, first: int, old_stop: int, new_stop: int) -> None:
        offsets: list[list[int]] = []
        tab_widths: list[list[int]] = []
        for index in range(first, new_stop):
            source = self.document.get_line(index)
            sections = get_tab_widths(source, self._tab_width)
            tab_widths.append([size for _, size in sections])
            if not self._width:
                offsets.append([])
            elif source.isascii():
                offsets.append(
                    compute_wrap_offsets(
                        source,
                        self._width,
                        self._tab_width,
                        precomputed_tab_sections=sections,
                    )
                )
            elif "\t" not in source:
                offsets.append(wrap_offsets(source, self._width))
            else:
                expanded, ends = _expand_tabs(source, tab_widths[-1])
                offsets.append(
                    sorted(
                        {
                            bisect_right(ends, offset)
                            for offset in wrap_offsets(expanded, self._width)
                            if 0 < bisect_right(ends, offset) < len(source)
                        }
                    )
                )
        self._wrap_offsets[first:old_stop] = offsets
        self._tab_width_cache[first:old_stop] = tab_widths
        visual_start = (
            self._line_index_to_offsets[first][0]
            if first < len(self._line_index_to_offsets)
            else len(self._offset_to_line_info)
        )
        del self._offset_to_line_info[visual_start:]
        del self._line_index_to_offsets[first:]
        for index in range(first, len(self._wrap_offsets)):
            height = len(self._wrap_offsets[index]) + 1
            self._line_index_to_offsets.append(list(range(visual_start, visual_start + height)))
            self._offset_to_line_info.extend((index, section) for section in range(height))
            visual_start += height


def _expand_tabs(source: str, tab_widths: list[int]) -> tuple[str, list[int]]:
    """Expand Textual's fixed tab widths and retain source-column boundaries."""
    parts = source.split("\t")
    expanded: list[str] = []
    lengths: list[int] = []
    for index, part in enumerate(parts):
        expanded.append(part)
        lengths.extend([1] * len(part))
        if index < len(parts) - 1:
            size = tab_widths[index]
            expanded.append(" " * size)
            lengths.append(size)
    return "".join(expanded), list(accumulate(lengths))
