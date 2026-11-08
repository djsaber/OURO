"""Bundled packages; importing the catalog loads metadata, never renderers."""

from ouro_tui_themes.packs.matrix import PACKAGE as MATRIX
from ouro_tui_themes.packs.particle_art import PACKAGE as PARTICLE_ART

PACKAGES = (PARTICLE_ART, MATRIX)
