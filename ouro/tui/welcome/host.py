"""Full-page package lifecycle and placement of the persistent input surface."""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.geometry import NULL_OFFSET, NULL_SPACING, Region, Size
from textual.layout import ArrangeResult, Layout, WidgetPlacement
from textual.layouts.vertical import VerticalLayout
from textual.widget import Widget

from ouro.tui.view_models import TuiSystemStatus
from ouro.tui.welcome.model import WelcomePage, WelcomeStyle


class _WelcomeLayout(Layout):
    """Resolve page and input in one layout pass, without reparenting input widgets."""

    def __init__(self, host: WelcomeHost) -> None:
        self.host = host
        self.vertical = VerticalLayout()

    def arrange(
        self, parent: Widget, children: list[Widget], size: Size, greedy: bool = True
    ) -> ArrangeResult:
        host = self.host
        if not host.welcome_active:
            return self.vertical.arrange(parent, children, size, greedy)
        if not size.width or not size.height:
            return []
        surface, page = host.input_surface, host.page

        def measure(width: int) -> int:
            if not isinstance(width, int) or not 1 <= width <= size.width:
                raise ValueError(f"{host.style_package.style_id}: invalid input width {width}")
            content_width = max(0, width - surface.styles.gutter.width)
            height = surface.get_content_height(
                Size(content_width, size.height), host.app.size, content_width
            )
            return min(size.height, max(1, height + surface.styles.gutter.height))

        region = page.place_input(size, measure)
        if (
            not isinstance(region, Region)
            or not all(isinstance(value, int) for value in region)
            or not region
            or region not in size.region
        ):
            raise ValueError(
                f"{host.style_package.style_id}: invalid input region for {size}: {region}"
            )
        page.input_region = region
        host.constrain_input(region, size)
        return [
            WidgetPlacement(size.region, NULL_OFFSET, NULL_SPACING, page),
            WidgetPlacement(region, NULL_OFFSET, NULL_SPACING, surface, order=1),
        ]


class WelcomeHost(Widget):
    """Keep interaction widgets mounted while replacing complete welcome pages."""

    DEFAULT_CSS = """
    WelcomeHost { width: 100%; height: auto; overflow: hidden; }
    WelcomeHost.welcome-page { height: 100%; }
    """

    def __init__(self, surface: Widget, style: WelcomeStyle) -> None:
        super().__init__(id="input-region")
        self.input_surface = surface
        self.style_package = style
        self.page = self._create(style)
        self.page.display = False
        self.welcome_active = False
        self._status = TuiSystemStatus()
        self._page_layout = _WelcomeLayout(self)

    @property
    def layout(self) -> Layout:
        return self._page_layout

    @staticmethod
    def _create(style: WelcomeStyle) -> WelcomePage:
        page = style.create()
        if not isinstance(page, WelcomePage) or page.is_attached:
            raise ValueError(f"{style.style_id}: factory must return a fresh WelcomePage")
        return page

    def compose(self) -> ComposeResult:
        yield self.page
        yield self.input_surface

    def on_mount(self) -> None:
        self.app.screen_change_signal.subscribe(self, lambda _: self._sync_active())
        self.page.set_system_status(self._status)
        self.page.refresh_theme()
        self._sync_active()

    def on_unmount(self) -> None:
        self.app.screen_change_signal.unsubscribe(self)
        self.page.set_active(False)

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self.page.set_system_status(status)

    async def set_welcome_style(self, style: WelcomeStyle) -> None:
        if style != self.style_package:
            page = self._create(style)
            page.display = False
            with self.app.batch_update():
                self.page.set_active(False)
                await asyncio.shield(self.page.remove())
                self.remove_class(f"welcome-style-{self.style_package.style_id}")
                self.style_package, self.page = style, page
                await self.mount(page, before=self.input_surface)
                self.set_class(self.welcome_active, f"welcome-style-{style.style_id}")
                page.display = self.welcome_active
                page.set_system_status(self._status)
                page.refresh_theme()
        self._sync_active()
        self.refresh(layout=True)

    def set_welcome_mode(self, enabled: bool) -> None:
        self.welcome_active = enabled
        self.set_class(enabled, "welcome-page")
        self.set_class(enabled, f"welcome-style-{self.style_package.style_id}")
        self.page.display = enabled
        self._sync_active()
        self.refresh(layout=True)

    def _sync_active(self) -> None:
        active = self.welcome_active and self.is_attached and self.app.screen is self.screen
        self.page.set_active(active)

    def constrain_input(self, region: Region, viewport: Size) -> None:
        """Let the host bound connected menus against the resolved input location."""
