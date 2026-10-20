"""Renderer-independent contracts for custom welcome style packages."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from textual.geometry import Region, Size
from textual.reactive import reactive
from textual.widget import Widget

from ouro.tui.controller import TuiSystemStatus
from ouro.tui.theme import TuiTheme, theme_for


class WelcomePage(Widget):
    """A package-owned page covering the entire welcome viewport.

    Compose arbitrary Textual children, use local CSS or a custom Layout, and
    render at any cell. The host overlays its persistent input surface at the
    region returned by place_input. Input state never belongs to a package.
    """

    DEFAULT_CSS = """
    WelcomePage { width: 100%; height: 100%; overflow: hidden; }
    """

    input_region = reactive(Region(), layout=True)
    """Host-resolved input rectangle, in page coordinates; read-only to packages."""

    def _watch_input_region(self) -> None:
        # Textual caches arrangements by page size and child membership. Input
        # growth changes neither, but package layouts may depend on this region.
        self._clear_arrangement_cache()

    def place_input(self, viewport: Size, measure: Callable[[int], int]) -> Region:
        """Return an in-bounds rectangle; measure(width) gives the input's height.

        This synchronous layout hook must not perform I/O or mutate widgets.
        Widths must be between one and viewport.width. The host clips measured
        height to the viewport so a short terminal still has an input surface.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement place_input")

    def set_system_status(self, status: TuiSystemStatus) -> None:
        """Receive the host's immutable system/usage snapshot without querying services."""

    def set_active(self, active: bool) -> None:
        """Receive foreground visibility; the package owns its resource policy."""

    @property
    def tui_theme(self) -> TuiTheme:
        """Current immutable TUI palette, including the user's saturation selection.

        Read after mounting or in refresh_theme; without an App palette this uses
        the default.
        """
        return theme_for(self)

    def refresh_theme(self) -> None:
        """Read tui_theme after mount or a palette change and refresh owned content.

        CSS using $ouro-* variables updates automatically. This hook also runs for
        hidden mounted pages; it must not activate the page or reset input state.
        """


@dataclass(frozen=True)
class WelcomeStyle:
    """A page factory with stable metadata and an optional dependency check.

    The check returns an unavailability reason, or None when usable. It runs
    once per App, shared by styles using the same check. Factories load optional
    renderers only when selected; metadata remains usable without them.
    """

    style_id: str
    label: str
    description: str
    create: Callable[[], WelcomePage]
    check_requirements: Callable[[], str | None] | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", self.style_id) is None:
            raise ValueError("welcome style IDs must use lowercase words separated by hyphens")
        if not self.label.strip() or not self.description.strip() or not callable(self.create):
            raise ValueError("welcome styles require a label, description and page factory")
