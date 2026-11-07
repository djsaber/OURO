"""The fixed startup surface, independent of welcome packages and user themes."""

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from ouro.tui.startup import (
    BACKGROUND,
    BRAND,
    CONNECTOR,
    EXIT_HINT,
    FOREGROUND,
    FRAME_INTERVAL,
    StartupStage,
    startup_frame,
    startup_lines,
)
from ouro.tui.widgets.version import VersionLabel


class StartupScreen(Screen):
    DEFAULT_CSS = """
    StartupScreen { align: center middle; layers: artwork footer; }
    StartupScreen > Vertical { height: auto; layer: artwork; }
    StartupScreen > Vertical Static { width: 1fr; height: auto; text-align: center; }
    StartupScreen > VersionLabel { dock: bottom; layer: footer; }
    StartupScreen #startup-brand { text-style: bold; }
    StartupScreen #startup-message { margin-top: 1; }
    """
    BINDINGS = [Binding("ctrl+c,ctrl+q", "app.quit", "Quit", show=False, priority=True)]

    def __init__(self) -> None:
        super().__init__()
        self.styles.background = BACKGROUND
        self._stage: StartupStage = "data"
        self._frame = 0
        self._brand = Static(BRAND, id="startup-brand")
        self._progress = Static()
        self._labels = Static(" ".join(label.center(7) for label in ("START", "DATA", "VIEW")))
        self._message = Static(EXIT_HINT, id="startup-message")
        self._message.styles.color = CONNECTOR
        self._art = Vertical(self._brand, self._progress, self._labels, self._message)
        self._art.styles.color = FOREGROUND

    def compose(self) -> ComposeResult:
        yield self._art
        yield VersionLabel()

    def on_mount(self) -> None:
        self._timer = self.set_interval(FRAME_INTERVAL, self._advance)
        self._frame = startup_frame()
        self._resize_art()

    def on_unmount(self) -> None:
        self._timer.stop()

    def on_resize(self, _event: events.Resize) -> None:
        self._resize_art()

    def _resize_art(self) -> None:
        compact = self.size.width < 23
        self._art.styles.width = min(self.size.width, 11 if compact else 23)
        self._brand.styles.height = 2 if compact else 3
        self._labels.display = not compact
        self._draw_nodes()

    def prepare_view(self) -> None:
        self._stage = "view"
        self._draw_nodes()

    def show_error(self, message: str) -> None:
        self._timer.stop()
        content = Text(message, style="#c17b7b")
        content.append(f"\n\n{EXIT_HINT}", style=FOREGROUND)
        self._message.update(content)

    def show_exiting(self) -> None:
        self._timer.stop()
        self._message.update(Text("Exiting…", style=FOREGROUND))

    def _advance(self) -> None:
        self._frame = startup_frame()
        self._draw_nodes()

    def _draw_nodes(self) -> None:
        lines = startup_lines(self._stage, self._frame, width=self.size.width)
        nodes = Text(lines[2 if self.size.width < 23 else 3])
        nodes.highlight_regex("─", CONNECTOR)
        self._progress.update(nodes, layout=False)
