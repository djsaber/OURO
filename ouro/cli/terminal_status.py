from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

TerminalOutput = Callable[[str], None]
SpinnerSemantic = Literal["mcp_connection", "tool_input", "tool_execution"]
StatusTone = Literal["success", "error", "warning", "neutral"]

_CLEAR_LINE = "\x1b[2K"
_RESET = "\x1b[0m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"
_MAGENTA = "\x1b[35m"
_MAX_STATUS_LENGTH = 160


@dataclass(frozen=True)
class SpinnerTheme:
    """Frames and timing for one user-visible activity meaning."""

    frames: tuple[str, ...]
    interval_seconds: float
    color: str


SPINNER_THEMES: dict[SpinnerSemantic, SpinnerTheme] = {
    "mcp_connection": SpinnerTheme(
        frames=("◐", "◓", "◑", "◒"),
        interval_seconds=0.12,
        color=_CYAN,
    ),
    "tool_input": SpinnerTheme(
        frames=("▹", "▸", "▹", "▸"),
        interval_seconds=0.16,
        color=_MAGENTA,
    ),
    "tool_execution": SpinnerTheme(
        frames=("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
        interval_seconds=0.08,
        color=_YELLOW,
    ),
}

_TONE_STYLE: dict[StatusTone, tuple[str, str]] = {
    "success": ("✓", _GREEN),
    "error": ("✗", _RED),
    "warning": ("!", _YELLOW),
    "neutral": ("·", _CYAN),
}


class TerminalStatusLine:
    """Own one replaceable terminal line and its asynchronous animation task."""

    def __init__(
        self,
        output_func: TerminalOutput,
        *,
        animated: bool | None = None,
    ) -> None:
        self.output_func = output_func
        self.animated = (
            output_func is print and sys.stdout.isatty() if animated is None else animated
        )
        terminal_columns = shutil.get_terminal_size(fallback=(100, 24)).columns
        self._message_limit = (
            max(20, min(_MAX_STATUS_LENGTH, terminal_columns - 4))
            if self.animated
            else _MAX_STATUS_LENGTH
        )
        self.semantic: SpinnerSemantic | None = None
        self.message = ""
        self._frame_index = 0
        self._animation_task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self.semantic is not None

    async def start(self, semantic: SpinnerSemantic, message: str) -> None:
        if self.semantic == semantic:
            self.update(message)
            return
        await self.clear()
        self.semantic = semantic
        self.message = _status_text(message, self._message_limit)
        self._frame_index = 0
        self._render_frame()
        if self.animated:
            self._animation_task = asyncio.create_task(
                self._animate(),
                name=f"ouro-spinner-{semantic}",
            )

    def update(self, message: str) -> None:
        if self.semantic is None:
            return
        self.message = _status_text(message, self._message_limit)

    async def finish(self, message: str, *, tone: StatusTone = "success") -> None:
        await self.clear()
        symbol, color = _TONE_STYLE[tone]
        rendered_symbol = f"{color}{symbol}{_RESET}" if self.animated else symbol
        self._write(f"{rendered_symbol} {_status_text(message, self._message_limit)}\n")

    async def clear(self) -> None:
        task = self._animation_task
        self._animation_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        had_status = self.semantic is not None
        self.semantic = None
        self.message = ""
        self._frame_index = 0
        if had_status and self.animated:
            self._write(f"\r{_CLEAR_LINE}")

    async def _animate(self) -> None:
        try:
            while self.semantic is not None:
                theme = SPINNER_THEMES[self.semantic]
                await asyncio.sleep(theme.interval_seconds)
                if self.semantic is None:
                    return
                self._frame_index = (self._frame_index + 1) % len(theme.frames)
                self._render_frame()
        except asyncio.CancelledError:
            raise
        except Exception:  # Terminal refresh failure must not escape as an orphan task error.
            return

    def _render_frame(self) -> None:
        semantic = self.semantic
        if semantic is None:
            return
        theme = SPINNER_THEMES[semantic]
        frame = theme.frames[self._frame_index]
        rendered_frame = f"{theme.color}{frame}{_RESET}" if self.animated else frame
        prefix = f"\r{_CLEAR_LINE}" if self.animated else ""
        suffix = "" if self.animated else "\n"
        self._write(f"{prefix}{rendered_frame} {self.message}{suffix}")

    def _write(self, text: str) -> None:
        if self.output_func is print:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        self.output_func(text)


def _status_text(value: str, limit: int) -> str:
    single_line = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "…"
