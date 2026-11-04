"""Digital rain: glyph streams falling through layered phosphor green.

A self-contained welcome theme. It shares nothing with the dot-matrix engine
and needs no NumPy: columns of falling glyphs are simulated directly on the
page's own terminal cells. Depth comes from four layers of entry brightness,
fall speed and trail length, every trail cools through one shared green ramp,
and only rows whose visible cells changed are repainted.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable, Iterable
from math import exp, floor, log
from random import Random
from time import monotonic

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.geometry import NULL_OFFSET, NULL_SPACING, Region, Size
from textual.layout import ArrangeResult, Layout, WidgetPlacement
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from ouro.tui.view_models import TuiSystemStatus
from ouro.tui.welcome.components import WelcomeSystem, WelcomeTips, WelcomeUsage, WelcomeVersion
from ouro.tui.welcome.model import WelcomePage
from ouro_tui_themes.rendering import FrameStrip, RunStyle, render_frame_lines

_FRAME_INTERVAL = 1 / 20
_MAX_TICK_SECONDS = 0.25
_CURSOR_HALF_PERIOD = 0.6
# Lower visual contrast independently of trail lifetime and brightness buckets.
# Rain remains continuous while foreground information owns the visual emphasis.
_RAIN_OPACITY = 0.35
# A clear cell between strands preserves their silhouette as trails lengthen,
# and bounds the amount of animated text sent through terminal composition.
_COLUMN_SPACING = 2
# Visible brightness steps; level 0 is the bare background and never renders.
_LEVELS = 32
_MAX_LEVEL = _LEVELS - 1
# Heads materialize over their first two rows instead of popping in.
_ENTRY_RAMP = (0.55, 0.85, 1.0)
# A full-brightness trail falls below the first visible bucket after this many
# glow seconds. Keep the column occupied until even its faintest tail has cooled.
_TRAIL_REACH = log(_LEVELS)


def _glyph_pool() -> tuple[str, ...]:
    """Weighted glyphs: a binary core, a Latin body, katakana and ASCII accents.

    Halfwidth katakana keeps the film's script texture while every glyph stays
    exactly one terminal cell wide; no emoji enter the pool.
    """

    pool = ["0", "1"] * 12
    pool += list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    pool += list("abcdefghijklmnopqrstuvwxyz")
    pool += list("23456789")
    pool += list("ｱｳｴｵｷｸｻｼｽｾｿﾀﾂﾃﾅﾆﾇﾊﾎﾏﾓﾗﾜﾝ")
    pool += list("$+*<>=#%&@")
    return tuple(pool)


_GLYPHS = _glyph_pool()
_GLYPH_COUNT = len(_GLYPHS)

# Depth layers, far to near: entry brightness, fall speed (cells/s), trail decay
# length (cells per e-fold), idle time before respawn (seconds) and spawn weight.
# Choose spatial length independently of speed: slow distant streams must still
# read as continuous strands, rather than cooling away after one or two cells.
_LAYERS: tuple[tuple[float, float, float, float, float, float, float, int], ...] = (
    (0.34, 3.5, 6.0, 9.0, 13.0, 0.9, 3.4, 30),
    (0.55, 5.5, 9.0, 12.0, 18.0, 1.1, 3.7, 28),
    (0.76, 8.0, 13.0, 16.0, 23.0, 1.1, 3.9, 24),
    (1.00, 11.0, 18.0, 20.0, 30.0, 1.4, 4.2, 18),
)
_LAYER_WEIGHTS = tuple(layer[7] for layer in _LAYERS)
_SPAWN_TOTAL = sum(_LAYER_WEIGHTS)

# The shared green spectrum. Faint levels sink toward the background, mids carry
# the phosphor hue, and only the hottest tip approaches a soft mint white.
_RAMP = (
    (0.00, (5, 24, 11)),
    (0.16, (10, 50, 24)),
    (0.34, (20, 104, 52)),
    (0.52, (38, 152, 86)),
    (0.70, (62, 198, 112)),
    (0.85, (128, 232, 162)),
    (1.00, (214, 255, 228)),
)


def _ramp(t: float) -> tuple[int, int, int]:
    for (low, low_color), (high, high_color) in zip(_RAMP, _RAMP[1:], strict=True):
        if t <= high:
            span = (t - low) / (high - low)
            return cast_rgb(
                round(begin + (end - begin) * span)
                for begin, end in zip(low_color, high_color, strict=True)
            )
    return _RAMP[-1][1]


def cast_rgb(channels: Iterable[float]) -> tuple[int, int, int]:
    """Round three channel values into one RGB tuple."""

    red, green, blue = (round(channel) for channel in channels)
    return red, green, blue


def _parse_hex(color: str) -> tuple[int, int, int]:
    return (
        int(color[1:3], 16),
        int(color[3:5], 16),
        int(color[5:7], 16),
    )


def _spectrum(background: tuple[int, int, int], *, monochrome: bool) -> tuple[Style | None, ...]:
    """One style per brightness level, blended onto the live page background.

    The mix factor follows the level, capped by the rain's overall opacity:
    distant streams sink into the background and even nearby tips remain muted.
    This changes contrast without shortening the simulated trails.
    """

    if monochrome:
        return (None,) * _LEVELS
    styles: list[Style | None] = [None]
    for level in range(1, _LEVELS):
        weight = level / _MAX_LEVEL
        blended = _blend(background, _ramp(weight), weight * _RAIN_OPACITY)
        styles.append(Style(color="#{:02x}{:02x}{:02x}".format(*blended)))
    return tuple(styles)


def _blend(
    low: tuple[int, int, int], high: tuple[int, int, int], amount: float
) -> tuple[int, int, int]:
    return cast_rgb(begin + (end - begin) * amount for begin, end in zip(low, high, strict=True))


class _Stream:
    """One falling column with its own depth, speed and trail decay."""

    __slots__ = ("brightness", "column", "glow", "head", "layer", "previous", "speed", "tail")

    def __init__(
        self, layer: int, column: int, head: float, speed: float, glow: float, brightness: float
    ) -> None:
        self.layer = layer
        self.column = column
        self.head = head
        self.speed = speed
        self.glow = glow
        self.brightness = brightness
        self.tail = glow * speed * _TRAIL_REACH + 1.0
        self.previous = floor(head)


class _Rainfield:
    """Column rain simulation over one cell per character; no UI or theme knowledge."""

    __slots__ = (
        "changed",
        "glyph",
        "height",
        "level",
        "streams",
        "width",
        "_cooldown",
        "_glow",
        "_lit",
        "_live",
        "_radiance",
        "_random",
        "_slot",
    )

    def __init__(self, width: int, height: int, seed: int | None = None) -> None:
        if width < 1 or height < 1:
            raise ValueError("a rain field needs a positive width and height")
        self.width = width
        self.height = height
        self._random = Random(seed)
        size = width * height
        self.level = bytearray(size)
        self.glyph = bytearray(size)
        self.changed = bytearray(height)
        self._radiance = array("f", bytes(4 * size))
        # A column owns one stream until its entire trail has cooled. All of its
        # cells therefore share a decay rate, including the off-screen head phase.
        self._glow = array("f", bytes(4 * width))
        self._slot: list[_Stream | None] = [None] * width
        self._cooldown = array("f", bytes(4 * width))
        self._lit = array("i")
        self._live = bytearray(size)
        self.streams: list[_Stream] = []
        for column in range(0, width, _COLUMN_SPACING):
            if self._random.random() < 0.55:
                self._spawn(column, seed=True)
            else:
                self._cooldown[column] = self._random.uniform(0.0, 2.0)

    def advance(self, dt: float) -> None:
        """Cool trails, spawn idle columns, move heads, then mutate a few glyphs."""

        width = self.width
        changed = self.changed
        lit = self._lit
        radiance, glow, level, live = self._radiance, self._glow, self.level, self._live
        glyph, random = self.glyph, self._random
        # 1) Burning trails cool toward the background in one compaction pass.
        decay = [exp(-dt / value) if value else 0.0 for value in glow]
        write = 0
        for read in range(len(lit)):
            index = lit[read]
            remaining = radiance[index] * decay[index % width]
            bucket = min(int(remaining * _LEVELS), _MAX_LEVEL)
            if not bucket:
                live[index] = 0
                radiance[index] = 0.0
                if level[index]:
                    level[index] = 0
                    changed[index // width] = 1
                continue
            radiance[index] = remaining
            if bucket != level[index]:
                level[index] = bucket
                changed[index // width] = 1
            if write != read:
                lit[write] = index
            write += 1
        del lit[write:]
        # 2) Columns whose slot freed up may spawn again after their idle time.
        slot, cooldown = self._slot, self._cooldown
        for column in range(0, width, _COLUMN_SPACING):
            remaining = cooldown[column] - dt
            if remaining > 0.0:
                cooldown[column] = remaining
            elif slot[column] is None:
                self._spawn(column)
        # 3) Heads advance, lighting the cells they cross.
        falling = []
        for stream in self.streams:
            head = stream.head + stream.speed * dt
            row = floor(head)
            if row > stream.previous:
                for cell_row in range(max(stream.previous + 1, 0), min(row, self.height - 1) + 1):
                    # Use the crossing time within this frame, so adjacent cells
                    # follow one continuous decay even when a head skips rows.
                    age = (head - cell_row) / stream.speed
                    self._light(
                        stream.column,
                        cell_row,
                        stream.brightness
                        * _ENTRY_RAMP[cell_row if cell_row < 2 else 2]
                        * exp(-age / stream.glow),
                    )
                stream.previous = row
            stream.head = head
            if head - stream.tail < self.height:
                falling.append(stream)
            else:
                slot[stream.column] = None
                layer = _LAYERS[stream.layer]
                cooldown[stream.column] = self._random.uniform(layer[5], layer[6])
        self.streams = falling
        # 4) A few burning glyphs mutate so the rain keeps flickering.
        if lit:
            for _ in range(min(len(lit), 2 + len(lit) // 48)):
                index = lit[random.randrange(len(lit))]
                glyph[index] = random.randrange(_GLYPH_COUNT)
                changed[index // width] = 1

    def _light(self, column: int, row: int, radiance: float) -> None:
        index = row * self.width + column
        bucket = min(int(radiance * _LEVELS), _MAX_LEVEL)
        if not bucket:
            return
        self._radiance[index] = radiance
        self.glyph[index] = self._random.randrange(_GLYPH_COUNT)
        self.level[index] = bucket
        self.changed[row] = 1
        if not self._live[index]:
            self._live[index] = 1
            self._lit.append(index)

    def _spawn(self, column: int, *, seed: bool = False) -> None:
        random = self._random
        pick = random.randrange(_SPAWN_TOTAL)
        layer_id = len(_LAYERS) - 1
        for index, weight in enumerate(_LAYER_WEIGHTS):
            pick -= weight
            if pick < 0:
                layer_id = index
                break
        layer = _LAYERS[layer_id]
        speed = random.uniform(layer[1], layer[2])
        glow = random.uniform(layer[3], layer[4]) / speed
        self._glow[column] = glow
        brightness = layer[0] * random.uniform(0.92, 1.0)
        tail = glow * speed * _TRAIL_REACH + 1.0
        stream = _Stream(layer_id, column, 0.0, speed, glow, brightness)
        if seed:
            stream.head = random.uniform(-tail, float(self.height))
            first = max(0, floor(stream.head - tail))
            for row in range(first, min(self.height, floor(stream.head) + 1)):
                age = (stream.head - row) / speed
                self._light(
                    column,
                    row,
                    brightness * _ENTRY_RAMP[row if row < 2 else 2] * exp(-age / glow),
                )
        else:
            stream.head = -random.uniform(0.0, 2.0)
        stream.previous = floor(stream.head)
        self._slot[column] = stream
        self.streams.append(stream)


class _MatrixLayout(Layout):
    """Place foreground content; unused space belongs to the page's rain field."""

    def arrange(
        self, parent: Widget, children: list[Widget], size: Size, greedy: bool = True
    ) -> ArrangeResult:
        assert isinstance(parent, MatrixPage)
        caption, usage, tips, system, version = children
        x, y, width, height = parent.input_region
        viewport = parent.app.size

        def content_width(widget: Widget, limit: int) -> int:
            return min(limit, widget.get_content_width(size, viewport))

        caption_width = content_width(caption, width)
        tips_width = min(width, max(20, content_width(tips, width)))
        usage_width = max(0, width - tips_width - 1)
        usage_height = usage.get_content_height(size, viewport, usage_width)
        tips_height = tips.get_content_height(size, viewport, tips_width)
        lower_height = max(usage_height, tips_height)
        system_height = system.get_content_height(size, viewport, width)
        # Share available breathing room between sections, without spacer widgets.
        available = max(0, size.height - (y + height) - 1)
        gap = max(0, (available - lower_height - system_height) // 2)
        lower_y = y + height + min(2, gap)
        system_y = lower_y + lower_height + min(3, gap)
        version_width = content_width(version, size.width)
        regions = (
            Region(
                x + (width - caption_width) // 2, max(0, y - 3), caption_width, 2 if y >= 3 else 0
            ),
            Region(x, lower_y, usage_width, usage_height),
            Region(x + width - tips_width, lower_y, tips_width, tips_height),
            Region(x, system_y, width, system_height),
            Region(max(0, size.width - version_width - 2), size.height - 1, version_width, 1),
        )
        return [
            WidgetPlacement(region, NULL_OFFSET, NULL_SPACING, child, order=index)
            for index, (child, region) in enumerate(zip(children, regions, strict=True))
        ]


class MatrixPage(WelcomePage):
    """Full-viewport rain beneath the shared welcome components and input."""

    DEFAULT_CSS = """
    /* Textual transparency blends colors, not glyphs. Layout-only containers
       stay in the layout but don't paint spaces over the page's character field.
       Explicitly visible text children retain rendering and mouse hit testing. */
    MatrixPage WelcomeInformation { visibility: hidden; }
    MatrixPage WelcomeInformation Static {
        visibility: visible; max-width: 100%; padding-right: 1;
    }
    MatrixPage WelcomeInformation .welcome-usage-line,
    MatrixPage WelcomeInformation .welcome-value { width: auto; }
    MatrixPage .welcome-label { width: 11; }
    MatrixPage .welcome-tip-key { width: 8; }
    MatrixPage .welcome-value { text-wrap: nowrap; }
    MatrixPage .matrix-signal {
        width: auto; height: 2; text-align: center; text-wrap: nowrap;
    }
    MatrixPage WelcomeVersion { padding: 0; width: auto; }
    """

    def __init__(self, *, seed: int | None = None) -> None:
        super().__init__()
        self._seed = seed
        self._caption = Static(classes="matrix-signal")
        self._cursor_visible = True
        self._usage = WelcomeUsage()
        self._tips = WelcomeTips()
        self._system = WelcomeSystem()
        self._page_layout = _MatrixLayout()
        self.active = False
        self._timer: Timer | None = None
        self._last_tick: float | None = None
        self._pending = False
        self._monochrome = False
        self._styles = _spectrum((12, 12, 12), monochrome=False)
        self._field: _Rainfield | None = None
        self._rows: list[Strip] = []
        self._render_style: Style | None = None
        self._width = 0
        self._height = 0

    @property
    def layout(self) -> Layout:
        return self._page_layout

    def compose(self) -> ComposeResult:
        yield self._caption
        yield self._usage
        yield self._tips
        yield self._system
        yield WelcomeVersion()

    def place_input(self, viewport: Size, measure: Callable[[int], int]) -> Region:
        column_width = min(viewport.width, 116, max(48, viewport.width * 9 // 10))
        width = max(1, column_width - 2)
        height = measure(width)
        return Region((viewport.width - width) // 2, (viewport.height - height) // 2, width, height)

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._system.set_system_status(status)
        self._usage.set_system_status(status)

    def on_mount(self) -> None:
        self._timer = self.set_interval(_FRAME_INTERVAL, self._tick, pause=True)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._last_tick = None
        self._pending = False
        self._field = None
        self._rows = []

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        self._last_tick = None
        if self._timer is not None:
            if active:
                self._timer.resume()
            else:
                self._timer.pause()

    def on_resize(self, event: events.Resize) -> None:
        self._ensure()
        self.refresh()

    def refresh_theme(self) -> None:
        theme = self.tui_theme
        self._monochrome = bool(self.app.no_color) if self.is_mounted else False
        self._styles = _spectrum(_parse_hex(theme.background), monochrome=self._monochrome)
        self._refresh_caption()
        self._repaint()

    def _refresh_caption(self) -> None:
        theme = self.tui_theme
        caption = Text("wake up, operator.\n", style=theme.accent, justify="center")
        caption.append("follow the white rabbit", style=theme.text_lowlight)
        caption.append("_" if self._cursor_visible else " ", style=theme.accent)
        # Preserve the cursor's cell while hidden; blinking never changes layout.
        self._caption.update(caption, layout=False)

    def render_line(self, y: int) -> Strip:
        style = self._render_style
        if style is None:
            self._ensure()
            style = self.rich_style
        rows = self._rows
        if 0 <= y < len(rows):
            return rows[y].apply_style(style)
        return Strip.blank(self.size.width, style)

    def render_lines(self, crop: Region) -> list[Strip]:
        # Preserve Textual CSS, filters and cropping before transferring
        # disposable frame rows to the compositor. Resolve inherited style and
        # size once per paint, not once per row; don't cache them across paints.
        self._ensure()
        self._render_style = self.rich_style
        try:
            if (lines := render_frame_lines(self, self._rows, crop)) is not None:
                return lines
            return [FrameStrip(line, line.cell_length) for line in super().render_lines(crop)]
        finally:
            self._render_style = None

    def _ensure(self) -> _Rainfield | None:
        """Build or rebuild the simulation when the viewport size changes."""

        size = self.size
        field = self._field
        if field is not None and (self._width, self._height) == (size.width, size.height):
            return field
        if size.width < 1 or size.height < 1:
            self._field, self._rows = None, []
            self._width = self._height = 0
            return None
        self._width, self._height = size.width, size.height
        field = _Rainfield(size.width, size.height, seed=self._seed)
        self._field = field
        self._rows = [self._render_row(row) for row in range(size.height)]
        field.changed[:] = bytes(size.height)
        self._pending = True
        return field

    def _tick(self) -> None:
        now = monotonic()
        delta = 0.0 if self._last_tick is None else min(now - self._last_tick, _MAX_TICK_SECONDS)
        self._last_tick = now
        field = self._ensure()
        if field is None:
            return
        if delta > 0.0:
            field.advance(delta)
        self._publish()
        cursor_visible = int(now / _CURSOR_HALF_PERIOD) % 2 == 0
        if cursor_visible != self._cursor_visible:
            self._cursor_visible = cursor_visible
            self._refresh_caption()

    def _publish(self) -> None:
        field = self._field
        if field is None:
            return
        changed = field.changed
        regions = []
        for row in range(self._height):
            if changed[row]:
                changed[row] = 0
                self._rows[row] = self._render_row(row)
                regions.append(Region(0, row, self._width, 1))
        if self._pending or len(regions) == self._height:
            self._pending = False
            self.refresh()
        elif regions:
            self.refresh(*regions)

    def _repaint(self) -> None:
        if self._field is None:
            return
        self._rows = [self._render_row(row) for row in range(self._height)]
        self.refresh()

    def _render_row(self, row: int) -> Strip:
        width = self._width
        field = self._field
        assert field is not None
        level = field.level
        start = row * width
        end = start + width
        if not any(level[start:end]):
            return FrameStrip.blank(width)
        glyph, styles = field.glyph, self._styles
        texts: list[str] = []
        colors: list[RunStyle] = []
        cells: list[str] = []
        current = level[start]
        for index in range(start, end):
            bucket = level[index]
            # Blank cells have no foreground ink. Keep them in the preceding
            # color run instead of emitting a reset and a new run for every gap.
            # CSS and terminal filters still apply normally to the resulting row.
            if bucket and bucket != current:
                texts.append("".join(cells))
                colors.append(styles[current])
                cells.clear()
                current = bucket
            cells.append(_GLYPHS[glyph[index]] if bucket else " ")
        texts.append("".join(cells))
        colors.append(styles[current])
        return FrameStrip.from_runs(texts, colors, width)


def create_page() -> MatrixPage:
    return MatrixPage()
