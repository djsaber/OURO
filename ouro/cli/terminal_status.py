from __future__ import annotations

import asyncio
import shutil
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

TerminalOutput = Callable[[str], None]
SpinnerSemantic = Literal[
    "mcp_connection",
    "model_wait",
    "compaction",
    "tool_input",
    "tool_execution",
]
StatusTone = Literal["success", "error", "warning", "neutral"]

_CLEAR_LINE = "\x1b[2K"
_CLEAR_TO_END = "\x1b[J"
_RESTORE_CURSOR = "\x1b[u"
_SAVE_CURSOR_AT_LINE_START = "\r\x1b[s"
_RESET = "\x1b[0m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"
_BLUE = "\x1b[34m"
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
        frames=("◜", "◝", "◞", "◟"),
        interval_seconds=0.12,
        color=_CYAN,
    ),
    "model_wait": SpinnerTheme(
        frames=("⁙", "⁘"),
        interval_seconds=0.2,
        color=_BLUE,
    ),
    "compaction": SpinnerTheme(
        frames=("⌜", "⌝", "⌟", "⌞"),
        interval_seconds=0.12,
        color=_BLUE,
    ),
    "tool_input": SpinnerTheme(
        frames=("◱", "◰", "◳", "◲"),
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
    "success": ("●", _GREEN),
    "error": ("●", _RED),
    "warning": ("●", _YELLOW),
    "neutral": ("●", _CYAN),
}


def tone_style(tone: StatusTone) -> tuple[str, str]:
    """Return the (symbol, color) pair for a status tone."""
    return _TONE_STYLE[tone]


class TerminalStatusLine:
    """Own one replaceable terminal line and its asynchronous animation task."""

    def __init__(
        self,
        output_func: TerminalOutput,
        *,
        animated: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.output_func = output_func
        self._clock = clock
        self.animated = _supports_in_place_output(output_func) if animated is None else animated
        terminal_columns = shutil.get_terminal_size(fallback=(100, 24)).columns
        self._message_limit = (
            max(20, min(_MAX_STATUS_LENGTH, terminal_columns - 4))
            if self.animated
            else _MAX_STATUS_LENGTH
        )
        self.semantic: SpinnerSemantic | None = None
        self.message = ""
        self._elapsed_started_at: float | None = None
        self._frame_index = 0
        self._animation_task: asyncio.Task[None] | None = None
        self._suspended = False

    @property
    def active(self) -> bool:
        return self.semantic is not None

    async def start(self, semantic: SpinnerSemantic, message: str) -> None:
        await self._start(semantic, message, timed=False)

    async def start_timed(self, semantic: SpinnerSemantic, message: str) -> None:
        """Start one animated phase whose elapsed seconds update with each frame."""

        await self._start(semantic, message, timed=True)

    async def _start(
        self,
        semantic: SpinnerSemantic,
        message: str,
        *,
        timed: bool,
    ) -> None:
        if self.semantic == semantic and not self._suspended:
            self.message = _status_text(message, self._message_limit)
            self._elapsed_started_at = self._clock() if timed else None
            if self.animated:
                self._render_frame()
            return
        await self.clear()
        self.semantic = semantic
        self.message = _status_text(message, self._message_limit)
        self._elapsed_started_at = self._clock() if timed else None
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
        self._elapsed_started_at = None
        self._frame_index = 0
        self._suspended = False
        if had_status and self.animated:
            self._write(f"\r{_CLEAR_LINE}")

    async def suspend(self) -> None:
        """Stop animating and erase the line while retaining the logical status.

        Unlike ``clear``, the semantic/message/elapsed baseline survive so ``resume``
        can restore the same status once a foreground prompt releases the terminal.
        """

        task = self._animation_task
        self._animation_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.semantic is not None:
            self._suspended = True
            if self.animated:
                self._write(f"\r{_CLEAR_LINE}")

    async def resume(self) -> None:
        """Redraw and re-animate a status previously paused by ``suspend``."""

        if not self._suspended or self.semantic is None:
            self._suspended = False
            return
        self._suspended = False
        self._render_frame()
        if self.animated:
            self._animation_task = asyncio.create_task(
                self._animate(),
                name=f"ouro-spinner-{self.semantic}",
            )

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
        message = self.message
        if self._elapsed_started_at is not None:
            elapsed = max(0.0, self._clock() - self._elapsed_started_at)
            message = _status_text(f"{message} · {elapsed:.1f}s", self._message_limit)
        self._write(f"{prefix}{rendered_frame} {message}{suffix}")

    def _write(self, text: str) -> None:
        if self.output_func is print:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        self.output_func(text)


@dataclass
class _StatusRegionRow:
    semantic: SpinnerSemantic
    message: str
    tone: StatusTone | None = None


class TerminalStatusRegion:
    """Own a stable group of terminal rows that can be updated by key."""

    def __init__(
        self,
        output_func: TerminalOutput,
        *,
        animated: bool | None = None,
    ) -> None:
        self.output_func = output_func
        self.animated = _supports_in_place_output(output_func) if animated is None else animated
        terminal_columns = shutil.get_terminal_size(fallback=(100, 24)).columns
        self._message_limit = (
            max(20, min(_MAX_STATUS_LENGTH, terminal_columns - 4))
            if self.animated
            else _MAX_STATUS_LENGTH
        )
        self._rows: dict[str, _StatusRegionRow] = {}
        self._visible_row_count = 0
        self._frame_index = 0
        self._animation_task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return bool(self._rows)

    async def upsert(
        self,
        key: str,
        semantic: SpinnerSemantic,
        message: str,
    ) -> None:
        row = _StatusRegionRow(
            semantic=semantic,
            message=_status_text(message, self._message_limit),
        )
        if self._rows.get(key) == row:
            return
        self._rows[key] = row
        if self.animated:
            self._render_rows()
            self._ensure_animation()

    async def finish(
        self,
        key: str,
        message: str,
        *,
        tone: StatusTone = "success",
    ) -> None:
        current = self._rows.get(key)
        semantic = current.semantic if current is not None else "tool_execution"
        self._rows[key] = _StatusRegionRow(
            semantic=semantic,
            message=_status_text(message, self._message_limit),
            tone=tone,
        )
        if self.animated:
            self._render_rows()
            if not any(row.tone is None for row in self._rows.values()):
                await self._stop_animation()

    async def suspend(self) -> None:
        """Temporarily erase the region while retaining its logical rows."""

        await self._stop_animation()
        self._erase_visible_rows()

    async def resume(self) -> None:
        """Redraw rows retained by ``suspend`` and re-animate any still-live row."""

        if not self._rows or not self.animated:
            return
        self._render_rows()
        if any(row.tone is None for row in self._rows.values()):
            self._ensure_animation()

    async def commit(self) -> None:
        """Leave one stable output row per key and release the region."""

        await self._stop_animation()
        if not self._rows:
            return
        if self.animated:
            self._render_rows()
            self._write("\n")
        else:
            for row in self._rows.values():
                self._write(f"{self._render_row(row)}\n")
        self._rows.clear()
        self._visible_row_count = 0
        self._frame_index = 0

    async def clear(self) -> None:
        await self._stop_animation()
        self._erase_visible_rows()
        self._rows.clear()
        self._frame_index = 0

    def _ensure_animation(self) -> None:
        if self._animation_task is not None:
            return
        self._animation_task = asyncio.create_task(
            self._animate(),
            name="ouro-status-region",
        )

    async def _stop_animation(self) -> None:
        task = self._animation_task
        self._animation_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _animate(self) -> None:
        try:
            while any(row.tone is None for row in self._rows.values()):
                intervals = [
                    SPINNER_THEMES[row.semantic].interval_seconds
                    for row in self._rows.values()
                    if row.tone is None
                ]
                await asyncio.sleep(min(intervals, default=0.1))
                self._frame_index += 1
                self._render_rows()
        except asyncio.CancelledError:
            raise
        except Exception:  # Terminal refresh failure must not escape as an orphan task error.
            return
        finally:
            if self._animation_task is asyncio.current_task():
                self._animation_task = None

    def _render_rows(self) -> None:
        if not self._rows:
            return
        old_count = self._visible_row_count
        parts: list[str] = []
        if old_count:
            parts.append("\r")
            if old_count > 1:
                parts.append(f"\x1b[{old_count - 1}A")
        for index, row in enumerate(self._rows.values()):
            parts.append(_CLEAR_LINE)
            parts.append(self._render_row(row))
            if index >= len(self._rows) - 1:
                continue
            if old_count and index < old_count - 1:
                parts.append("\x1b[1B\r")
            else:
                parts.append("\n")
        self._write("".join(parts))
        self._visible_row_count = len(self._rows)

    def _erase_visible_rows(self) -> None:
        count = self._visible_row_count
        if not self.animated or count == 0:
            self._visible_row_count = 0
            return
        parts = ["\r"]
        if count > 1:
            parts.append(f"\x1b[{count - 1}A")
        for index in range(count):
            parts.append(_CLEAR_LINE)
            if index < count - 1:
                parts.append("\x1b[1B\r")
        if count > 1:
            parts.append(f"\x1b[{count - 1}A")
        self._write("".join(parts))
        self._visible_row_count = 0

    def _render_row(self, row: _StatusRegionRow) -> str:
        if row.tone is not None:
            symbol, color = _TONE_STYLE[row.tone]
        else:
            theme = SPINNER_THEMES[row.semantic]
            symbol = theme.frames[self._frame_index % len(theme.frames)]
            color = theme.color
        rendered_symbol = f"{color}{symbol}{_RESET}" if self.animated else symbol
        return f"{rendered_symbol} {row.message}"

    def _write(self, text: str) -> None:
        if self.output_func is print:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        self.output_func(text)


class TerminalTransientRegion:
    """Own terminal content that disappears when a foreground interaction ends.

    A prompt may contain several rendered lines, wrapped input, and validation
    feedback, so counting rows is fragile. On an interactive terminal the region
    instead saves its cursor origin and clears everything below that origin when
    released. Event rendering is paused by the caller while the region is active,
    which guarantees the erased area contains only temporary interaction UI.

    Redirected and injected output is intentionally left intact: without cursor
    control there is no safe way to erase it, and retaining prompts keeps logs and
    tests readable.
    """

    def __init__(
        self,
        output_func: TerminalOutput,
        *,
        animated: bool | None = None,
    ) -> None:
        self.output_func = output_func
        self.animated = _supports_in_place_output(output_func) if animated is None else animated
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def begin(self) -> None:
        if self._active:
            raise RuntimeError("transient terminal region is already active")
        self._active = True
        if self.animated:
            self._write(_SAVE_CURSOR_AT_LINE_START)

    def clear(self) -> None:
        if not self._active:
            return
        if self.animated:
            self._write(f"{_RESTORE_CURSOR}{_CLEAR_TO_END}")
        self._active = False

    def _write(self, text: str) -> None:
        if self.output_func is print:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        self.output_func(text)


def _supports_in_place_output(output_func: TerminalOutput) -> bool:
    return output_func is print and sys.stdout.isatty()


def _status_text(value: str, limit: int) -> str:
    single_line = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "…"
