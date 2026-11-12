"""Welcome style catalog, independent of the interface color theme."""

from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType

from ouro.tui.welcome.basic import STYLE as BASIC_STYLE
from ouro.tui.welcome.model import ThemePackage, WelcomePage, WelcomeStyle

WELCOME_STYLES = MappingProxyType({BASIC_STYLE.style_id: BASIC_STYLE})
DEFAULT_WELCOME_STYLE = "basic"


def welcome_catalog(packages: Iterable[ThemePackage] = ()) -> Mapping[str, WelcomeStyle]:
    """Index themes once per App, rejecting duplicate package and theme identities."""
    catalog = dict(WELCOME_STYLES)
    package_ids: set[str] = set()
    for package in packages:
        if package.package_id in package_ids:
            raise ValueError(f"duplicate theme package: {package.package_id}")
        package_ids.add(package.package_id)
        for style in package.themes:
            if style.style_id in catalog:
                raise ValueError(f"duplicate welcome style: {style.style_id}")
            catalog[style.style_id] = style
    return MappingProxyType(catalog)


class WelcomeRequirements:
    """Check optional dependencies on demand, sharing results within one App."""

    def __init__(self) -> None:
        self._results: dict[Callable[[], str | None], str | None] = {}

    def reason(self, style: WelcomeStyle) -> str | None:
        check = style.check_requirements
        if check is None:
            return None
        if check not in self._results:
            self._results[check] = check()
        return self._results[check]

    def check_all(self, styles: Iterable[WelcomeStyle]) -> None:
        for style in styles:
            self.reason(style)


__all__ = [
    "DEFAULT_WELCOME_STYLE",
    "WELCOME_STYLES",
    "WelcomeStyle",
    "ThemePackage",
    "WelcomePage",
    "welcome_catalog",
    "WelcomeRequirements",
]
