"""Compare the animation adapter against Textual's CSS and cell semantics."""

from __future__ import annotations

import weakref

import pytest

pytest.importorskip("ouro_tui_themes")

from ouro_tui_themes.packs import PACKAGES
from ouro_tui_themes.tools.rendering import FrameStrip, render_frame_lines
from rich.ansi import AnsiDecoder
from rich.color import Color as RichColor
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.terminal_theme import DEFAULT_TERMINAL_THEME
from textual.app import App
from textual.cache import FIFOCache
from textual.color import Color
from textual.filter import ANSIToTruecolor, DimFilter, Monochrome
from textual.geometry import Region
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input

from ouro.tui.rendering import TuiCompositor
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.welcome import welcome_catalog
from ouro.tui.welcome.host import WelcomeHost


class Canvas(Widget):
    DEFAULT_CSS = "Canvas {width:12; height:4; background:#182331; color:#aa9988;}"

    def __init__(self):
        super().__init__()
        self.rows = [
            FrameStrip(
                [Segment("a界b", Style(color="#53aa82", bold=True)), Segment("12345678")], 12
            )
            for _ in range(4)
        ]

    def render_line(self, y):
        return self.rows[y].apply_style(self.rich_style)

    def render_lines(self, crop):
        lines = render_frame_lines(self, self.rows, crop)
        return lines if lines is not None else super().render_lines(crop)


class CanvasApp(App):
    def compose(self):
        yield Canvas()


@pytest.mark.parametrize("filter_type", [None, Monochrome, DimFilter])
@pytest.mark.parametrize("crop", [Region(0, 0, 12, 4), Region(2, 1, 7, 2)])
async def test_frame_canvas_matches_textual_styles_filters_and_wide_cell_cropping(
    filter_type, crop
):
    app = CanvasApp()
    async with app.run_test():
        if filter_type is not None:
            app._filters.append(filter_type())
        canvas = app.query_one(Canvas)
        for background in ("#182331", "#eeddbb"):
            canvas.styles.background = background
            canvas._styles_cache.clear()
            expected = Widget.render_lines(canvas, crop)
            actual = canvas.render_lines(crop)
            assert actual == expected
            assert [row.cell_length for row in actual] == [crop.width] * crop.height
            assert [row.render(app.console) for row in actual] == [
                row.render(app.console) for row in expected
            ]


@pytest.mark.parametrize(
    "name,value",
    [
        ("border", ("round", "red")),
        ("padding", (1, 2)),
        ("outline", ("solid", "blue")),
        ("opacity", 0.6),
        ("text_opacity", 0.4),
        ("tint", "red 20%"),
        ("hatch", ("/", "red")),
    ],
)
async def test_decorated_canvas_uses_textual_renderer(name, value):
    app = CanvasApp()
    async with app.run_test() as pilot:
        canvas = app.query_one(Canvas)
        setattr(canvas.styles, name, value)
        await pilot.pause()
        crop = Region(0, 0, 12, 4)
        assert render_frame_lines(canvas, canvas.rows, crop) is None
        assert canvas.render_lines(crop) == Widget.render_lines(canvas, crop)


@pytest.mark.parametrize("start,end", [(0, 12), (1, 2), (2, 3), (3, 8), (-1, 30), (15, 20)])
def test_transient_rows_match_native_cell_cuts_and_release(start, end):
    segments = [Segment("a界b", Style(color="red")), Segment("12345678")]
    row = FrameStrip(segments, 12)
    original = Strip(segments, 12)
    assert row.crop(start, end) == original.crop(start, end)
    assert row.divide([1, 2, 3, 12, 20]) == original.divide([1, 2, 3, 12, 20])
    assert row.divide([12])[0] is row
    reference = weakref.ref(row)
    del row
    assert reference() is None


async def test_dynamic_canvas_avoids_per_row_transform_cache_allocation(monkeypatch):
    app = CanvasApp()
    async with app.run_test():
        canvas = app.query_one(Canvas)
        allocations = 0
        initialize = FIFOCache.__init__

        def counted(self, *args, **kwargs):
            nonlocal allocations
            allocations += 1
            initialize(self, *args, **kwargs)

        monkeypatch.setattr(FIFOCache, "__init__", counted)
        crop = Region(1, 0, 10, 4)
        canvas._styles_cache.clear()
        expected = Widget.render_lines(canvas, crop)
        native_allocations = allocations
        allocations = 0
        assert canvas.render_lines(crop) == expected
        assert native_allocations > 0
        assert allocations < native_allocations // 4


@pytest.mark.parametrize(
    "source",
    [
        Style(color="#58aa63"),
        Style(color="red"),
        Style(color="#58aa63", dim=True),
        Style(color="#58aa63", meta={"@click": "open"}),
    ],
)
def test_deferred_style_encoding_and_filters_match_native_strip(source):
    segments = [Segment("a界b", source), Segment("cd")]
    base = Style(color="#aaaaaa", bgcolor="#102030", bold=True)
    overlay = Style(italic=True, bgcolor="#304050")
    console = Console(color_system="truecolor")
    background = Color.parse("#102030")
    filter = ANSIToTruecolor(DEFAULT_TERMINAL_THEME)
    for cuts in ([6], [1, 3, 6]):
        original = Strip(segments, 6).apply_style(base).apply_style(overlay)
        frame = FrameStrip(segments, 6).apply_style(base).apply_style(overlay)
        original = original.apply_filter(filter, background)
        frame = frame.apply_filter(filter, background)
        # Encode before inspecting _segments, to exercise deferred output.
        assert [part.render(console) for part in frame.divide(cuts)] == [
            part.render(console) for part in original.divide(cuts)
        ]
        assert list(frame) == list(original)


def test_truecolor_output_does_not_materialize_styled_segment_copies(monkeypatch):
    row = FrameStrip([Segment("abc", Style(color="#46895a"))], 3)
    original_new = Segment.__new__
    allocations = 0

    def counted(*args, **kwargs):
        nonlocal allocations
        allocations += 1
        return original_new(*args, **kwargs)

    monkeypatch.setattr(Segment, "__new__", staticmethod(counted))
    styled = row.apply_style(Style(bgcolor="#102030"))
    filtered = styled.apply_filter(ANSIToTruecolor(DEFAULT_TERMINAL_THEME), Color.parse("#102030"))
    console = Console(color_system="truecolor")
    assert "abc" in filtered.divide([3])[0].render(console)
    assert allocations == 0
    # A consumer asking for segments still gets the conventional styled values.
    assert list(filtered)[0].style.bgcolor.get_truecolor() == (16, 32, 48)


@pytest.mark.parametrize("color_system", ["truecolor", "256", "standard"])
@pytest.mark.parametrize("filter_type", [None, Monochrome, DimFilter])
def test_packed_runs_match_rich_encoding_filters_and_cell_cuts(color_system, filter_type):
    texts = ["abｱ", "⠁⠂", " ", "z"]
    styles = [0x0011FE, 0xABCDEF, Style(), 0x000000]
    base = Style(color="#98af34", bgcolor="#183456", bold=True, italic=True, underline=True)
    native = Strip(
        [
            Segment(
                text,
                Style(color=RichColor.from_rgb(value >> 16, (value >> 8) & 255, value & 255))
                if isinstance(value, int)
                else value,
            )
            for text, value in zip(texts, styles, strict=True)
        ],
        7,
    ).apply_style(base)
    frame = FrameStrip.from_runs(texts, styles, 7).apply_style(base)
    if filter_type:
        filter = filter_type()
        native = native.apply_filter(filter, Color.parse("#183456"))
        frame = frame.apply_filter(filter, Color.parse("#183456"))
    for cuts in ([7], [0, 2, 3, 4, 6, 7]):
        console = Console(color_system=color_system)
        bounds = ((0, 6), (1, 7), (1, 6), (1, 2), (3, 4), (4, 4), (-1, 30), (10, 12))
        actual = [*frame.divide(cuts), *(frame.crop(start, end) for start, end in bounds)]
        expected = [*native.divide(cuts), *(native.crop(start, end) for start, end in bounds)]
        for row, reference in zip(actual, expected, strict=True):
            actual_decoder, expected_decoder = AnsiDecoder(), AnsiDecoder()
            # Compact SGR retains state between RGB runs; compare decoded
            # characters/styles and the final terminal state, not redundant bytes.
            assert actual_decoder.decode_line(row.render(console)) == expected_decoder.decode_line(
                reference.render(console)
            )
            assert actual_decoder.style == expected_decoder.style
        assert list(actual) == list(expected)


def test_packed_frame_pipeline_allocates_no_per_run_rich_objects(monkeypatch):
    base = Style(bgcolor="#102030")
    console = Console(color_system="truecolor")
    # Warm only the stable canvas style, then use unrelated colors in every run.
    FrameStrip.from_runs(["x"], [0x123456], 1).apply_style(base).render(console)

    def forbidden(*args, **kwargs):
        pytest.fail("the packed frame path must not construct per-run Rich objects")

    monkeypatch.setattr(Segment, "__new__", staticmethod(forbidden))
    monkeypatch.setattr(Style, "__init__", forbidden)
    monkeypatch.setattr(RichColor, "from_rgb", forbidden)
    row = FrameStrip.from_runs(["ab"] * 160, list(range(160)), 320).apply_style(base)
    row = row.apply_filter(ANSIToTruecolor(DEFAULT_TERMINAL_THEME), Color.parse("#102030"))
    parts = row.crop(1, 319).divide([13, 67, 318])
    assert all(part.render(console) for part in parts)


@pytest.mark.parametrize("style", ["matrix", "event-horizon", "quantum-veil"])
@pytest.mark.parametrize("monochrome", [False, True])
async def test_theme_frames_match_native_compositor_after_resize(style, monochrome, monkeypatch):
    if monochrome:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    class ThemeApp(App):
        def get_css_variables(self):
            return {**super().get_css_variables(), **DEFAULT_TUI_THEME.css_variables()}

        def compose(self):
            host = WelcomeHost(Input("draft"), welcome_catalog(PACKAGES)[style])
            host.set_welcome_mode(True)
            yield host

    app = ThemeApp()
    async with app.run_test(size=(100, 40)) as pilot:
        page = app.query_one(WelcomeHost).page
        page.set_active(False)
        animation = getattr(page, "matrix", page)
        optimized_render = animation.render_lines
        for width, height in ((100, 40), (45, 20), (160, 50)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            # Both renders use the same frozen simulation, foreground and layout.
            animation.render_lines = lambda crop: Widget.render_lines(animation, crop)
            reference = app.screen._compositor.render_strips()
            animation.render_lines = optimized_render
            original_compositor = app.screen._compositor
            compositor = TuiCompositor()
            compositor.reflow(app.screen, app.screen.size)
            app.screen._compositor = compositor
            actual = app.screen._compositor.render_strips()
            app.screen._compositor = original_compositor
            assert actual == reference
            assert [row.render(app.console) for row in actual] == [
                row.render(app.console) for row in reference
            ]
