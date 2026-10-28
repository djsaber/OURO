"""Compose the optional official distribution with the welcome framework.

Only this application boundary knows the installed distribution's name. Framework
contracts and preference storage remain usable without extension code or NumPy.
"""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import cast

from packaging.version import InvalidVersion, Version

from ouro.tui.welcome import WelcomeStyle

THEME_PACKAGE_VERSION = "0.3.6.post2"
INSTALL_HINT = "Install ouro-agent[themes] in the OURO environment and restart the TUI."


def load_theme_styles() -> tuple[tuple[WelcomeStyle, ...], str | None]:
    """Load lightweight metadata for the supported theme release, once per App."""
    try:
        installed = version("ouro-tui-themes")
    except PackageNotFoundError:
        return (), f"ouro-tui-themes is not installed. {INSTALL_HINT}"
    try:
        compatible = Version(installed) >= Version(THEME_PACKAGE_VERSION)
    except InvalidVersion:
        compatible = False
    if not compatible:
        return (), (
            f"ouro-tui-themes {installed} is incompatible; "
            f"requires >={THEME_PACKAGE_VERSION}. {INSTALL_HINT}"
        )
    try:
        package = import_module("ouro_tui_themes.styles")
    except ModuleNotFoundError as exc:
        if exc.name not in {"ouro_tui_themes", "ouro_tui_themes.styles"}:
            raise
        return (), f"ouro-tui-themes could not load: {exc}. {INSTALL_HINT}"
    return cast(tuple[WelcomeStyle, ...], package.STYLES), None
