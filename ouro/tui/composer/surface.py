"""Welcome-page surface that hosts the shared input composer."""

from __future__ import annotations

from textual.containers import Vertical
from textual.geometry import Region, Size

from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import COMMAND_POPUP_MAX_HEIGHT, COMMAND_POPUP_MIN_HEIGHT, CommandPopup
from ouro.tui.composer.queue import QueuedInputPanel
from ouro.tui.interactions.approval import (
    INTERACTION_PANEL_MAX_HEIGHT,
    INTERACTION_PANEL_MIN_HEIGHT,
)
from ouro.tui.interactions.bar import InteractionBar
from ouro.tui.interactions.plan import PlanConfirmationPanel
from ouro.tui.interactions.questions import AskUserPanel
from ouro.tui.subagent_navigation import SubagentNavigationPanel
from ouro.tui.view_models import TuiSystemStatus
from ouro.tui.welcome import DEFAULT_WELCOME_STYLE, WELCOME_STYLES, WelcomeStyle
from ouro.tui.welcome.host import WelcomeHost
from ouro.tui.widgets.status import StatusLine

# The regular surface always retains the Host header, one conversation row, the
# fixed input separator/navigation row, and the status line around the composer.
# The composer contributes its own current height.
_REGULAR_SURFACE_CHROME_HEIGHT = 4


class WelcomePanel(WelcomeHost):
    """Connect welcome page placement to the shared Host interaction surface."""

    def __init__(
        self,
        status: TuiSystemStatus | None = None,
        *,
        welcome_style: WelcomeStyle = WELCOME_STYLES[DEFAULT_WELCOME_STYLE],
    ) -> None:
        self._subagent_navigation = SubagentNavigationPanel()
        self._command_popup = CommandPopup()
        self._interaction_bar = InteractionBar()
        self._composer = Composer()
        self._composer.display = False
        surface = Vertical(
            self._subagent_navigation,
            self._command_popup,
            AskUserPanel(),
            PlanConfirmationPanel(),
            self._interaction_bar,
            self._composer,
            QueuedInputPanel(),
            StatusLine(),
            id="interaction-surface",
        )
        surface.styles.height = "auto"
        super().__init__(surface, welcome_style)
        self._status = status or TuiSystemStatus()
        self._viewport_size: Size | None = None

    def on_mount(self) -> None:
        self.set_viewport_size(self.app.screen.size)
        self.watch(self._composer, "virtual_size", self._apply_viewport_constraints, init=False)

    def set_welcome_mode(self, enabled: bool) -> None:
        super().set_welcome_mode(enabled)
        self._command_popup.styles.overlay = "screen" if enabled else "none"
        if not enabled:
            self._command_popup.styles.offset = (0, 0)
        self._apply_viewport_constraints()

    def set_viewport_size(self, size: Size) -> None:
        if size != self._viewport_size:
            self._viewport_size = size
            self._apply_viewport_constraints()

    def constrain_input(self, region: Region, viewport: Size) -> None:
        above, below = region.y, viewport.height - region.bottom
        place_above = above >= COMMAND_POPUP_MIN_HEIGHT or above >= below
        self._bound_command_popup(max(1, above if place_above else below))
        self._command_popup.styles.offset = (0, "-100%" if place_above else region.height)

    def _bound_command_popup(self, available: int) -> None:
        self._command_popup.styles.max_height = min(COMMAND_POPUP_MAX_HEIGHT, available)
        self._command_popup.styles.min_height = min(COMMAND_POPUP_MIN_HEIGHT, available)

    def _apply_viewport_constraints(self) -> None:
        size = self._viewport_size
        if size is None:
            return
        available = max(
            1, size.height - _REGULAR_SURFACE_CHROME_HEIGHT - self._composer.projected_height
        )
        if not self.welcome_active:
            self._bound_command_popup(available)
        self._interaction_bar.styles.max_height = min(INTERACTION_PANEL_MAX_HEIGHT, available)
        self._interaction_bar.styles.min_height = min(INTERACTION_PANEL_MIN_HEIGHT, available)
        self.refresh(layout=True)
