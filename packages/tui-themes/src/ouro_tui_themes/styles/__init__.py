"""Bundled theme metadata and lazy loading of each theme-owned page."""

from functools import partial
from importlib import import_module
from typing import cast

from ouro.tui.welcome.model import WelcomePage, WelcomeStyle
from ouro_tui_themes.dot_matrix.requirements import check_requirements


def _create_page(module: str) -> WelcomePage:
    # Only the selected theme loads its page and optional computation dependencies.
    theme = import_module(f"ouro_tui_themes.styles.{module}")
    return cast(WelcomePage, theme.create_page())


def _particle_style(module: str, style_id: str, label: str, description: str) -> WelcomeStyle:
    return WelcomeStyle(
        style_id,
        label,
        description,
        partial(_create_page, module),
        check_requirements=check_requirements,
    )


STYLES = (
    _particle_style(
        "event_horizon", "event-horizon", "Horizon", "An ember disk folded around a dark sun."
    ),
    _particle_style(
        "electron_bloom", "electron-bloom", "Bloom", "Ice and copper petals exchanging light."
    ),
    _particle_style(
        "interference", "interference", "Tide", "Jade light woven into a breathing caustic."
    ),
    _particle_style("clifford", "clifford-torus", "Iris", "An iris of folded ultraviolet light."),
    _particle_style(
        "crystal", "quasicrystal", "Prism", "An aperiodic rose cut from copper and light."
    ),
)
