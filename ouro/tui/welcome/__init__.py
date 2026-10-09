"""Welcome style catalog, independent of the interface color theme."""

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from ouro.tui.welcome.model import WelcomePage, WelcomeStyle
from ouro.tui.welcome.styles import (
    basic,
    clifford,
    crystal,
    electron_bloom,
    event_horizon,
    interference,
    julia_reliquary,
)

WELCOME_STYLES = MappingProxyType(
    {
        style.style_id: style
        for style in (
            basic.STYLE,
            event_horizon.STYLE,
            electron_bloom.STYLE,
            julia_reliquary.STYLE,
            interference.STYLE,
            clifford.STYLE,
            crystal.STYLE,
        )
    }
)
DEFAULT_WELCOME_STYLE = event_horizon.STYLE.style_id


def welcome_catalog(styles: Iterable[WelcomeStyle] = ()) -> Mapping[str, WelcomeStyle]:
    """Create an App-local catalog; custom packages cannot replace existing IDs."""
    catalog = dict(WELCOME_STYLES)
    for style in styles:
        if style.style_id in catalog:
            raise ValueError(f"duplicate welcome style: {style.style_id}")
        catalog[style.style_id] = style
    return MappingProxyType(catalog)


__all__ = [
    "DEFAULT_WELCOME_STYLE",
    "WELCOME_STYLES",
    "WelcomeStyle",
    "WelcomePage",
    "welcome_catalog",
]
