"""Shared composition for the panoramic particle themes; no sampling policy."""

from collections.abc import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.geometry import NULL_OFFSET, NULL_SPACING, Region, Size
from textual.layout import ArrangeResult, Layout, WidgetPlacement
from textual.widget import Widget
from textual.widgets import Static

from ouro import __version__
from ouro.tui.composer.input import COMPOSER_MIN_HEIGHT
from ouro.tui.view_models import TuiSystemStatus
from ouro.tui.welcome.components import WelcomeVersion
from ouro.tui.welcome.model import WelcomePage
from ouro.tui.widgets.status import StatusLine
from ouro_tui_themes.tools.dot_matrix.widget import DotMatrix

# VersionLabel includes two cells of padding on either side.
_VERSION_WIDTH = len(f"v{__version__}") + 4


class _ParticleWorkspace(Static):
    """Theme-owned trailing item in the host's horizontal status row."""

    DEFAULT_CSS = """
    _ParticleWorkspace {
        width: auto; max-width: 36%; height: 1; margin-left: 2;
        text-align: right; text-wrap: nowrap; text-overflow: ellipsis;
    }
    """


class _ParticleLayout(Layout):
    """Uninterrupted artwork above the bottom input, with a fixed version."""

    def arrange(
        self, parent: Widget, children: list[Widget], size: Size, greedy: bool = True
    ) -> ArrangeResult:
        assert isinstance(parent, ParticlePage)
        input_region = parent.input_region
        # The version never takes space from the input or wraps to another row.
        # Only show the complete label when two cells of separation also fit.
        version_region = (
            Region(size.width - _VERSION_WIDTH, size.height - 1, _VERSION_WIDTH, 1)
            if size.height and size.width - input_region.right >= _VERSION_WIDTH + 2
            else Region()
        )
        # One gap row above the single-line composer and its status row. Extra
        # input rows cover the artwork via the host's existing foreground layer,
        # without resizing the field or rebuilding its numerical buffers.
        artwork_bottom = input_region.bottom - (COMPOSER_MIN_HEIGHT + 1) - 1
        regions = (
            Region(0, 0, size.width, max(0, artwork_bottom)),
            # Preserve VersionLabel's own two-cell padding. The last glyph is
            # always at the same screen column as StartupScreen's version.
            version_region,
        )
        return [
            WidgetPlacement(region, NULL_OFFSET, NULL_SPACING, child, order=index)
            for index, (child, region) in enumerate(zip(children, regions, strict=True))
        ]


class ParticlePage(WelcomePage):
    """Artwork, native input and workspace sharing the status row."""

    DEFAULT_CSS = """
    ParticlePage WelcomeVersion { text-wrap: nowrap; overflow: hidden; }
    """

    def __init__(self, matrix: DotMatrix) -> None:
        super().__init__()
        self.matrix = matrix
        self.artwork = matrix.artwork
        self._workspace = _ParticleWorkspace(classes="particle-workspace")
        self._workspace_path = "not set"
        self._page_layout = _ParticleLayout()

    @property
    def layout(self) -> Layout:
        return self._page_layout

    def compose(self) -> ComposeResult:
        yield self.matrix
        yield WelcomeVersion()

    def on_mount(self) -> None:
        self.watch(self, "input_region", self._sync_workspace, init=False)
        self.call_after_refresh(self._attach_workspace)

    async def _attach_workspace(self) -> None:
        if not self.is_mounted or self.parent is None:
            return
        status = next(iter(self.parent.query(StatusLine)), None)
        if status is not None:
            # Add only our own label; existing host widgets and input stay put.
            await status.mount(self._workspace)
            self._sync_workspace()

    async def on_unmount(self) -> None:
        await self._workspace.remove()

    def _sync_workspace(self) -> None:
        self._workspace.display = self.display and self.input_region.width >= 80

    def place_input(self, viewport: Size, measure: Callable[[int], int]) -> Region:
        column_width = min(viewport.width, 116, max(48, viewport.width * 9 // 10))
        width = max(1, column_width - 2)
        x = (viewport.width - width) // 2
        height = min(measure(width), viewport.height)
        return Region(
            x,
            max(0, viewport.height - height),
            width,
            height,
        )

    def set_system_status(self, status: TuiSystemStatus) -> None:
        path = status.workspace or "not set"
        if path != self._workspace_path:
            self._workspace_path = path
            self.refresh_theme()

    def set_active(self, active: bool) -> None:
        self.matrix.set_active(active)
        self._sync_workspace()

    def refresh_theme(self) -> None:
        self._workspace.update(Text(self._workspace_path, style=self.tui_theme.text_lowlight))
