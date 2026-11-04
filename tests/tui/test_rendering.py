"""Guard the Textual 8 compositor and editor cache lifetime adapters."""

import weakref

import pytest
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual.cache import FIFOCache
from textual.strip import Strip

from ouro.tui.rendering import RenderLineCache, _CompositorStrip


@pytest.mark.parametrize("cuts", [[8], [1, 2, 3, 8], [3, 6], [0, 8, 20]])
def test_borrowed_rows_preserve_cells_styles_and_terminal_output(cuts):
    segments = [Segment("a界b", Style(color="red", link="https://example.com")), Segment("1234")]
    original = Strip(segments, 8)
    borrowed = _CompositorStrip(original)
    console = Console(color_system="truecolor")
    expected = original.divide(cuts)
    actual = borrowed.divide(cuts)
    assert list(actual) == list(expected)
    assert [row.render(console) for row in actual] == [row.render(console) for row in expected]
    assert borrowed.link_ids == original.link_ids
    assert borrowed.crop(2, 6) == original.crop(2, 6)


def test_borrowed_full_width_division_releases_source_without_gc():
    class Segments(list):
        pass

    source = Strip([Segment("abc")], 3)
    source._segments = Segments(source._segments)
    reference = weakref.ref(source._segments)
    borrowed = _CompositorStrip(source)
    assert borrowed.divide([3])[0] is borrowed
    assert not source._divide_cache
    del source, borrowed
    assert reference() is None


def test_compositor_fragments_do_not_allocate_transformation_caches(monkeypatch):
    source = Strip([Segment("a界bcdef")], 8)

    def forbidden(*args, **kwargs):
        pytest.fail("composition fragments must not allocate persistent caches")

    monkeypatch.setattr(FIFOCache, "__init__", forbidden)
    row = _CompositorStrip(source)
    console = Console(color_system="truecolor")
    assert "".join(part.render(console) for part in row.divide([1, 3, 8])) == source.text
    assert row.crop(3, 5).text == "bc"


def test_editor_cache_preserves_lru_eviction_and_releases_rows_on_clear():
    class Row(Strip):
        __slots__ = ("__weakref__",)

    cache = RenderLineCache(2)
    first, second, third = (Row([Segment(text)]) for text in ("a", "b", "c"))
    references = [weakref.ref(row) for row in (first, second, third)]
    cache[(1,)] = first
    cache[(2,)] = second
    assert cache[(1,)] is first
    cache[(3,)] = third
    assert (2,) not in cache
    assert cache[(1,)] is first
    del first, second, third
    assert references[1]() is None
    cache.clear()
    assert not cache
    assert all(reference() is None for reference in references)
    cache.clear()
    cache[(4,)] = Row([Segment("d")])
    assert cache[(4,)].text == "d"
    cache.clear()
