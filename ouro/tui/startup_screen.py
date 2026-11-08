"""The fixed startup surface, independent of welcome packages and user themes."""

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from ouro.tui.startup import (
    BACKGROUND,
    BRAND,
    EXIT_HINT,
    FOREGROUND,
    FRAME_INTERVAL,
    MUTED,
    startup_frame,
    startup_message,
)
from ouro.tui.widgets.version import VersionLabel


class StartupScreen(Screen):
    DEFAULT_CSS = """
    StartupScreen { align: center middle; layers: artwork footer; }
    StartupScreen > Vertical { height: auto; max-width: 100%; layer: artwork; }
    StartupScreen > VersionLabel { dock: bottom; layer: footer; }
    StartupScreen > Vertical Static { width: 1fr; height: auto; text-align: center; }
    StartupScreen #startup-brand { height: 1; margin-bottom: 1; text-style: bold; }
    StartupScreen #startup-message { text-wrap: nowrap; text-overflow: clip; }
    """
    BINDINGS = [Binding("ctrl+c,ctrl+q", "app.quit", "Quit", show=False, priority=True)]

    def __init__(self) -> None:
        super().__init__()
        self.styles.background = BACKGROUND
        self._brand = Static(BRAND, id="startup-brand")
        self._message = Static(startup_message(startup_frame()), id="startup-message")
        self._message.styles.color = MUTED
        self._art = Vertical(self._brand, self._message)
        self._art.styles.width = len(startup_message(0))
        self._art.styles.color = FOREGROUND

    def compose(self) -> ComposeResult:
        yield self._art
        yield VersionLabel()

    def on_mount(self) -> None:
        self._advance()
        self._timer = self.set_interval(FRAME_INTERVAL, self._advance)

    def on_unmount(self) -> None:
        self._timer.stop()

    def show_error(self, message: str) -> None:
        self._timer.stop()
        self._message.styles.text_wrap = "wrap"
        content = Text(message, style="#c17b7b")
        content.append(f"\n\n{EXIT_HINT}", style=MUTED)
        self._message.update(content)

    def show_exiting(self) -> None:
        self._timer.stop()
        self._message.update(Text("Exiting…", style=FOREGROUND))

    def _advance(self) -> None:
        message = startup_message(startup_frame())
        content = Text(message[:2], style=FOREGROUND)
        content.append(message[2:], style=MUTED)
        self._message.update(content, layout=False)
