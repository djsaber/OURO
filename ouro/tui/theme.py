"""Theme color tokens for the OURO TUI.

This module is the single source of truth for the accent color so the whole
interface recolors by changing one value. ``THEME_ACCENT`` is injected into the
``$ouro-theme`` CSS variable in ``app.py`` and referenced directly by Rich-text
styling in ``widgets.py`` / ``screens.py`` — borders, titles, the logo, active
highlights and selected markers all derive from it.

``THEME_ACCENT_BRIGHT`` lifts the accent for emphasized Rich-text headings and
selected labels, matching how a focused title reads above body text.
"""

from __future__ import annotations

# Primary accent: borders, titles, the logo, active highlights, selected markers.
THEME_ACCENT = "#5b8fd9"
# Bright variant: emphasized headings and selected labels in Rich text.
THEME_ACCENT_BRIGHT = "#659ff2"
