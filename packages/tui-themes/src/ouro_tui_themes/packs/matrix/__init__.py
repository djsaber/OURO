"""The Matrix metadata; glyph rain does not require the particle engine."""

from ouro.tui.welcome.model import ThemePackage, WelcomePage, WelcomeStyle


def _matrix() -> WelcomePage:
    from ouro_tui_themes.packs.matrix.matrix import MatrixPage

    return MatrixPage()


PACKAGE = ThemePackage(
    package_id="matrix",
    name="The Matrix",
    description="Digital rain inspired by the world of The Matrix.",
    author="OURO contributors",
    themes=(
        WelcomeStyle(
            "matrix",
            "Matrix",
            "Glyph rain falling through layered phosphor green.",
            _matrix,
        ),
    ),
)
