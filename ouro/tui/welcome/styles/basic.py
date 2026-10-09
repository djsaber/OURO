"""The quiet, typographic OURO welcome package."""

from rich.text import Text
from textual.widgets import Static

from ouro.tui.theme import theme_for
from ouro.tui.welcome.centered import CenteredWelcomePage
from ouro.tui.welcome.model import WelcomeStyle

_WORDMARK = (
    " ██████╗ ▒▒╗   ▒▒╗██████╗  ▒▒▒▒▒▒╗\n"
    "██╔═══██╗▒▒║   ▒▒║██╔══██╗▒▒╔═══▒▒╗\n"
    "██║   ██║▒▒║   ▒▒║██████╔╝▒▒║   ▒▒║\n"
    "██║   ██║▒▒║   ▒▒║██╔══██╗▒▒║   ▒▒║\n"
    "╚██████╔╝╚▒▒▒▒▒▒╔╝██║  ██║╚▒▒▒▒▒▒╔╝\n"
    " ╚═════╝  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝"
)


class BasicPage(CenteredWelcomePage):
    DEFAULT_CSS = """
    BasicPage .wordmark { width: auto; height: auto; margin-bottom: 2; }
    """

    def __init__(self) -> None:
        self._wordmark = Static(classes="wordmark")
        super().__init__(self._wordmark)

    def on_mount(self) -> None:
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self._wordmark.update(Text(_WORDMARK, style=f"bold {theme_for(self).accent}"))


STYLE = WelcomeStyle("basic", "Basic", "The OURO wordmark. Still and silent.", BasicPage)
