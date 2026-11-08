"""Particle Art metadata and lazy page factories."""

from ouro.tui.welcome.model import ThemePackage, WelcomePage, WelcomeStyle
from ouro_tui_themes.tools.dot_matrix.requirements import check_requirements


def _horizon() -> WelcomePage:
    from ouro_tui_themes.packs.particle_art.event_horizon import HorizonPage

    return HorizonPage()


def _quantum_veil() -> WelcomePage:
    from ouro_tui_themes.packs.particle_art.quantum_veil import QuantumPage

    return QuantumPage()


PACKAGE = ThemePackage(
    package_id="particle-art",
    name="Particle Art",
    description="Luminous particle fields shaped by gravity and wave interference.",
    author="OURO contributors",
    themes=(
        WelcomeStyle(
            "event-horizon",
            "Horizon",
            "An ember disk folded around a dark sun.",
            _horizon,
            check_requirements=check_requirements,
        ),
        WelcomeStyle(
            "quantum-veil",
            "Quantum veil",
            "Coherent waves woven into blue-green light.",
            _quantum_veil,
            check_requirements=check_requirements,
        ),
    ),
)
