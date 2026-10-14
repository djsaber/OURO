"""Dependency and loading boundary for the bundled dot-matrix themes."""

from functools import partial
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion

from ouro.tui.welcome.model import WelcomePage, WelcomeStyle

_ARRAYFUNC_VERSIONS = SpecifierSet(">=8.5.7,<9")
_INSTALL_HINT = "Install ouro-agent[themes] in the OURO environment and restart the TUI."


def check_requirements() -> str | None:
    """Check the actual extension, not just the presence of package metadata."""
    try:
        installed = version("arrayfunc")
    except PackageNotFoundError:
        return f"arrayfunc is not installed. {_INSTALL_HINT}"
    try:
        compatible = installed in _ARRAYFUNC_VERSIONS
    except InvalidVersion:
        compatible = False
    if not compatible:
        return (
            f"arrayfunc {installed} is incompatible; requires {_ARRAYFUNC_VERSIONS}. "
            f"{_INSTALL_HINT}"
        )
    try:
        import_module("arrayfunc")
    except (ImportError, OSError) as exc:
        return f"arrayfunc could not load: {exc}. {_INSTALL_HINT}"
    return None


def _create_page(module: str, label: str) -> WelcomePage:
    # Renderer imports belong to the selected feature, not TUI startup.
    from ouro.tui.welcome.dot_matrix.field import DotArtwork
    from ouro.tui.welcome.dot_matrix.widget import DotWelcomePage

    artwork = import_module(f"ouro.tui.welcome.styles.{module}")
    return DotWelcomePage(DotArtwork(label, artwork.prepare))


def dot_style(module: str, style_id: str, label: str, description: str) -> WelcomeStyle:
    return WelcomeStyle(
        style_id,
        label,
        description,
        partial(_create_page, module, label),
        check_requirements=check_requirements,
    )
