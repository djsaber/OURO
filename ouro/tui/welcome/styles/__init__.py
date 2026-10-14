"""Bundled welcome theme metadata, independent of optional renderers."""

from ouro.tui.welcome.dot_matrix import dot_style
from ouro.tui.welcome.styles.basic import STYLE as BASIC_STYLE

STYLES = (
    BASIC_STYLE,
    dot_style(
        "event_horizon", "event-horizon", "Horizon", "An ember disk folded around a dark sun."
    ),
    dot_style(
        "electron_bloom", "electron-bloom", "Bloom", "Ice and copper petals exchanging light."
    ),
    dot_style(
        "julia_reliquary",
        "julia-reliquary",
        "Labyrinth",
        "A violet labyrinth with a molten gold edge.",
    ),
    dot_style("interference", "interference", "Tide", "Jade light woven into a breathing caustic."),
    dot_style("clifford", "clifford-torus", "Iris", "An iris of folded ultraviolet light."),
    dot_style("crystal", "quasicrystal", "Prism", "An aperiodic rose cut from copper and light."),
)
