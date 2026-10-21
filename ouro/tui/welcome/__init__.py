"""Welcome style catalog, independent of the interface color theme."""

from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType

from ouro.tui.welcome.basic import STYLE as BASIC_STYLE
from ouro.tui.welcome.model import WelcomePage, WelcomeStyle

WELCOME_STYLES = MappingProxyType({BASIC_STYLE.style_id: BASIC_STYLE})
DEFAULT_WELCOME_STYLE = "basic"


def welcome_catalog(styles: Iterable[WelcomeStyle] = ()) -> Mapping[str, WelcomeStyle]:
    """Create an App-local catalog; custom packages cannot replace existing IDs."""
    catalog = dict(WELCOME_STYLES)
    for style in styles:
        if style.style_id in catalog:
            raise ValueError(f"duplicate welcome style: {style.style_id}")
        catalog[style.style_id] = style
    return MappingProxyType(catalog)


def unavailable_styles(styles: Mapping[str, WelcomeStyle]) -> Mapping[str, str]:
    """Evaluate each shared dependency check once for this App's catalog."""
    checks: dict[Callable[[], str | None], str | None] = {}
    unavailable = {}
    for identity, style in styles.items():
        check = style.check_requirements
        if check is None:
            continue
        if check not in checks:
            checks[check] = check()
        if reason := checks[check]:
            unavailable[identity] = reason
    return MappingProxyType(unavailable)


__all__ = [
    "DEFAULT_WELCOME_STYLE",
    "WELCOME_STYLES",
    "WelcomeStyle",
    "WelcomePage",
    "welcome_catalog",
    "unavailable_styles",
]
