"""Bundled theme metadata and lazy loading of each theme-owned page."""

from functools import partial
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import cast

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion

from ouro.tui.welcome.model import WelcomePage, WelcomeStyle
from ouro.tui.welcome.styles.basic import STYLE as BASIC_STYLE

_NUMPY_VERSIONS = SpecifierSet(">=2.3.3,<3")
_INSTALL_HINT = "Install ouro-agent[themes] in the OURO environment and restart the TUI."


def check_requirements() -> str | None:
    """Check the actual extension, not just the presence of package metadata."""
    try:
        installed = version("numpy")
    except PackageNotFoundError:
        return f"numpy is not installed. {_INSTALL_HINT}"
    try:
        compatible = installed in _NUMPY_VERSIONS
    except InvalidVersion:
        compatible = False
    if not compatible:
        return f"numpy {installed} is incompatible; requires {_NUMPY_VERSIONS}. {_INSTALL_HINT}"
    try:
        import_module("numpy")
    except (ImportError, OSError) as exc:
        return f"numpy could not load: {exc}. {_INSTALL_HINT}"
    return None


def _create_page(module: str) -> WelcomePage:
    # Only the selected theme loads its page and optional computation dependencies.
    theme = import_module(f"ouro.tui.welcome.styles.{module}")
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
    BASIC_STYLE,
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
