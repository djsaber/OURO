"""Quantum veil composition and vertical pointer control."""

from textual import events

from ouro_tui_themes.packs.particle_art.particle_page import ParticlePage
from ouro_tui_themes.packs.particle_art.quantum_field import QuantumScene
from ouro_tui_themes.tools.dot_matrix.raster import DotArtwork
from ouro_tui_themes.tools.dot_matrix.widget import DotMatrix


class _QuantumMatrix(DotMatrix):
    def __init__(self, artwork: DotArtwork, scene: QuantumScene) -> None:
        super().__init__(artwork)
        self._scene = scene

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.active and self.size.height > 1:
            self._scene.pointer_y = max(-1.0, min(1.0, 1 - 2 * event.y / (self.size.height - 1)))

    def on_leave(self, _event: events.Leave) -> None:
        self._scene.pointer_y = 0.0

    def set_active(self, active: bool) -> None:
        if not active:
            self._scene.pointer_y = 0.0
        super().set_active(active)


class QuantumPage(ParticlePage):
    def __init__(self) -> None:
        self._scene = QuantumScene()
        artwork = DotArtwork(self._scene.prepare, span=(4.0, 2.7))
        super().__init__(_QuantumMatrix(artwork, self._scene))
