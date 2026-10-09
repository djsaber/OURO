"""An optional centered page composition shared by the bundled art packages."""

from __future__ import annotations

from collections.abc import Callable

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import NULL_OFFSET, NULL_SPACING, Region, Size
from textual.layout import ArrangeResult, Layout, WidgetPlacement
from textual.widget import Widget
from textual.widgets import Static

from ouro.tui.controller import TuiSystemStatus
from ouro.tui.welcome.components import WelcomeSystem, WelcomeTips, WelcomeUsage, WelcomeVersion
from ouro.tui.welcome.model import WelcomePage


class _CenteredLayout(Layout):
    def __init__(self, page: CenteredWelcomePage) -> None:
        self.page = page

    def arrange(
        self, parent: Widget, children: list[Widget], size: Size, greedy: bool = True
    ) -> ArrangeResult:
        page = self.page
        x, y, width, height = page.input_region
        regions = (
            Region(x, 0, width, y),
            Region(x, y + height, width, max(0, size.height - y - height)),
            Region(0, max(0, size.height - 1), size.width, 1),
        )
        return [
            WidgetPlacement(region, NULL_OFFSET, NULL_SPACING, child, order=index)
            for index, (child, region) in enumerate(zip(children, regions, strict=True))
        ]


class CenteredWelcomePage(WelcomePage):
    """Reusable composition, not a restriction imposed by the welcome host."""

    DEFAULT_CSS = """
    CenteredWelcomePage .welcome-top { align: center bottom; }
    CenteredWelcomePage .welcome-bottom { padding-bottom: 1; overflow: hidden; }
    CenteredWelcomePage .welcome-gap { height: 1fr; min-height: 1; }
    CenteredWelcomePage .welcome-section-gap { max-height: 2; }
    CenteredWelcomePage .welcome-system-gap { max-height: 3; }
    CenteredWelcomePage .welcome-lower { width: 100%; height: auto; overflow: hidden; }
    """

    def __init__(self, artwork: Widget) -> None:
        super().__init__()
        self._top = Vertical(artwork, classes="welcome-top")
        self._system = WelcomeSystem()
        self._usage = WelcomeUsage()
        self._tips = WelcomeTips()
        self._lower = Horizontal(self._usage, self._tips, classes="welcome-lower")
        self._bottom = Vertical(
            Static(classes="welcome-gap welcome-section-gap"),
            self._lower,
            Static(classes="welcome-gap welcome-system-gap"),
            self._system,
            classes="welcome-bottom",
        )
        self._page_layout = _CenteredLayout(self)

    @property
    def layout(self) -> Layout:
        return self._page_layout

    def compose(self) -> ComposeResult:
        yield self._top
        yield self._bottom
        yield WelcomeVersion()

    def place_input(self, viewport: Size, measure: Callable[[int], int]) -> Region:
        column_width = min(viewport.width, 116, max(48, viewport.width * 9 // 10))
        width = max(1, column_width - 2)
        height = measure(width)
        return Region((viewport.width - width) // 2, (viewport.height - height) // 2, width, height)

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._system.set_system_status(status)
        self._usage.set_system_status(status)
