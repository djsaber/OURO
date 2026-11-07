"""Dependency-free startup artwork and terminal ownership before Textual starts."""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from threading import Event, Thread
from time import monotonic
from typing import Literal

from ouro import __version__
from ouro.tui.terminal import set_terminal_title, tui_terminal_guard

type StartupStage = Literal["start", "data", "view"]

BRAND = "O U R O"
BACKGROUND = "#0c0c0c"
FOREGROUND = "#ababab"
CONNECTOR = "#454545"
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
FRAME_INTERVAL = 0.12
EXIT_HINT = "Ctrl+C exit"


def startup_frame() -> int:
    """Keep the animation phase continuous across terminal ownership changes."""
    return int(monotonic() / FRAME_INTERVAL)


def startup_lines(stage: StartupStage, frame: int, *, width: int = 23) -> tuple[str, ...]:
    art_width = 11 if width < 23 else 23
    # Textual pads an odd spare cell on the right; str.center pads it on the left.
    brand = (" " * ((art_width - len(BRAND)) // 2) + BRAND).ljust(art_width)
    active = ("start", "data", "view").index(stage)
    markers = [
        "●"
        if index < active
        else SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
        if index == active
        else "○"
        for index in range(3)
    ]
    if width < 23:
        return (brand, "", " ".join(markers).center(11), "", EXIT_HINT)
    return (
        brand,
        "",
        "",
        "─".join(marker.center(7) for marker in markers),
        " ".join(label.center(7) for label in ("START", "DATA", "VIEW")),
        "",
        EXIT_HINT.center(23),
    )


class TerminalStartup:
    """Draw the first frame before imports; stop writing before the TUI driver takes over."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._resources = ExitStack()
        self._entered = False
        self._stop = Event()
        self._thread: Thread | None = None
        self._stream = sys.stdout
        self._size: os.terminal_size | None = None
        self._stage: StartupStage = "start"
        self._lines: tuple[str, ...] = ()

    def __enter__(self) -> TerminalStartup:
        try:
            if self._enabled:
                self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self, *, stage: StartupStage = "start") -> None:
        if self._thread is not None:
            return
        if not self._entered:
            self._resources.enter_context(
                tui_terminal_guard(input_stream=sys.stdin, output_stream=self._stream)
            )
            self._entered = True
            set_terminal_title("OURO", stream=self._stream)
        if not self._stream.isatty() or not sys.stdin.isatty():
            return
        self._stop.clear()
        self._stage = stage
        self._size = None
        self._stream.write("\x1b[?1049h\x1b[?25l")
        self._draw()
        self._thread = Thread(target=self._animate, name="ouro-startup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    @property
    def active(self) -> bool:
        return self._thread is not None

    def close(self) -> None:
        self.stop()
        self._resources.close()

    def _animate(self) -> None:
        while not self._stop.wait(FRAME_INTERVAL):
            self._draw()

    def _draw(self) -> None:
        size = os.get_terminal_size(self._stream.fileno())
        lines = startup_lines(self._stage, startup_frame(), width=size.columns)
        width = max(map(len, lines))
        row = max(1, (size.lines - len(lines)) // 2 + 1)
        column = max(1, (size.columns - width) // 2 + 1)
        # The fixed palette is monochrome, as is Textual's NO_COLOR projection.
        output = ["\x1b[48;2;12;12;12m"]
        resized = size != self._size
        if resized:
            # Erase-in-display may use the terminal's default background (not the
            # active SGR color), notably through ConPTY. Paint every cell instead.
            output.extend(
                f"\x1b[{line};1H" + " " * size.columns for line in range(1, size.lines + 1)
            )
            self._size = size
            self._lines = ()
        for index, line in enumerate(lines[: size.lines]):
            if index < len(self._lines) and line == self._lines[index]:
                continue
            output.append(f"\x1b[{row + index};{column}H")
            output.append("\x1b[1m" if index == 0 else "\x1b[22m")
            output.append(
                "\x1b[38;2;69;69;69m" if index == len(lines) - 1 else "\x1b[38;2;171;171;171m"
            )
            visible = line[: size.columns].replace(
                "─", "\x1b[38;2;69;69;69m─\x1b[38;2;171;171;171m"
            )
            output.append(visible)
        if resized:
            version = f"v{__version__}"
            version_column = max(1, size.columns - len(version) - 1)
            output.append(
                f"\x1b[{size.lines};{version_column}H\x1b[22m\x1b[38;2;103;103;103m{version}"
            )
        output.append("\x1b[0m")
        self._stream.write("".join(output))
        self._stream.flush()
        self._lines = lines
