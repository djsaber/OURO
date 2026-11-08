"""Horizon composition and horizontal pointer control."""

from textual import events

from ouro_tui_themes.packs.particle_art.horizon_field import HorizonScene
from ouro_tui_themes.packs.particle_art.particle_page import ParticlePage
from ouro_tui_themes.tools.dot_matrix.raster import DotArtwork
from ouro_tui_themes.tools.dot_matrix.widget import DotMatrix


class _HorizonMatrix(DotMatrix):
    """Coalesce pointer events into one theme-local scalar target."""

    def __init__(self, artwork: DotArtwork, scene: HorizonScene) -> None:
        super().__init__(artwork)
        self._scene = scene

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.active and self.size.width > 1:
            self._scene.pointer_x = max(-1.0, min(1.0, 2 * event.x / (self.size.width - 1) - 1))

    def on_leave(self, _event: events.Leave) -> None:
        self._scene.pointer_x = 0.0

    def set_active(self, active: bool) -> None:
        if not active:
            self._scene.pointer_x = 0.0
        super().set_active(active)


class HorizonPage(ParticlePage):
    def __init__(self) -> None:
        self._scene = HorizonScene()
        artwork = DotArtwork(self._scene.prepare, span=(5.8, 2.7))
        super().__init__(_HorizonMatrix(artwork, self._scene))
