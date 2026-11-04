"""Textual 8 adapters for the lifetime of rendered rows and editor caches."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from rich.console import Console
from rich.segment import Segment
from textual._compositor import Compositor
from textual.cache import LRUCache
from textual.geometry import Region
from textual.strip import Strip


class _CompositorStrip(Strip):
    """Frame-owned composition fragments, without persistent transform caches.

    Only divide/crop/render are used after the widget-to-compositor boundary.
    Full rows borrow native encoding caches; real cuts own just their segments.
    """

    __slots__ = ("_source",)

    def __init__(self, source: Strip) -> None:
        self._initialize(source._segments, source.cell_length, source)

    def _initialize(self, segments: list[Segment], width: int, source: Strip | None = None) -> None:
        self._source = source
        self._segments = segments
        self._cell_length = width
        self._render_cache = None
        self._link_ids = None
        self._cell_count = None

    def divide(self, cuts: Iterable[int]) -> Sequence[Strip]:
        cuts = tuple(cut for cut in cuts if cut <= self.cell_length)
        if cuts == (self.cell_length,):
            return (self,)
        parts = []
        start = 0
        for segments, end in zip(Segment.divide(self._segments, cuts), cuts, strict=False):
            part = _CompositorStrip.__new__(_CompositorStrip)
            part._initialize(segments, end - start)
            parts.append(part)
            start = end
        return parts

    def crop(self, start: int, end: int | None = None) -> Strip:
        start = max(0, start)
        end = self.cell_length if end is None else min(self.cell_length, end)
        if start == 0 and end == self.cell_length:
            return self
        if end <= start:
            part = _CompositorStrip.__new__(_CompositorStrip)
            part._initialize([], 0)
            return part
        return self.divide((start, end))[-1] if start else self.divide((end,))[0]

    def render(self, console: Console) -> str:
        return self._source.render(console) if self._source is not None else super().render(console)


class TuiCompositor(Compositor):
    """Preserve native composition while releasing transient widget rows promptly."""

    def _get_renders(
        self, crop: Region | None = None
    ) -> Iterable[tuple[Region, Region, list[Strip]]]:
        for region, clip, strips in super()._get_renders(crop):
            yield (
                region,
                clip,
                [_CompositorStrip(line) if type(line) is Strip else line for line in strips],
            )


class RenderLineCache(LRUCache[tuple, Strip]):
    """Keep TextArea's LRU policy but detach its ring when edits invalidate it."""

    def clear(self) -> None:
        # Textual drops the dictionary and head without unlinking the ring. On
        # every keystroke that leaves old rows and their caches for cyclic GC.
        for link in self._cache.values():
            link[0] = link[1] = None
        super().clear()
