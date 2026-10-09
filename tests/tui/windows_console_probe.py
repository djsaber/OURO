"""Native output probe, run in a child process with its own hidden console."""

from __future__ import annotations

import ctypes
import json
import msvcrt
import os
from ctypes import wintypes
from threading import Event, Thread

from ouro.tui import windows_console
from ouro.tui.windows_console import ConsoleOutput


class Coord(ctypes.Structure):
    _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT)]


class Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.SHORT),
        ("top", wintypes.SHORT),
        ("right", wintypes.SHORT),
        ("bottom", wintypes.SHORT),
    ]


class BufferInfo(ctypes.Structure):
    _fields_ = [
        ("size", Coord),
        ("cursor", Coord),
        ("attributes", wintypes.WORD),
        ("window", Rect),
        ("maximum", Coord),
    ]


def main() -> None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateConsoleScreenBuffer
    create.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    create.restype = wintypes.HANDLE
    set_mode = kernel.SetConsoleMode
    set_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    set_size = kernel.SetConsoleScreenBufferSize
    set_size.argtypes = [wintypes.HANDLE, Coord]
    set_window = kernel.SetConsoleWindowInfo
    set_window.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(Rect)]
    get_info = kernel.GetConsoleScreenBufferInfo
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(BufferInfo)]
    read = kernel.ReadConsoleOutputCharacterW
    read.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        Coord,
        ctypes.POINTER(wintypes.DWORD),
    ]

    handle = create(0xC0000000, 3, None, 1, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    # fdopen owns the new, inactive buffer. It never becomes the user's display.
    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    with open(fd, "w", encoding="utf-8") as stream:
        assert set_mode(handle, 7), ctypes.get_last_error()
        assert set_size(handle, Coord(120, 40)), ctypes.get_last_error()
        assert set_window(handle, True, ctypes.byref(Rect(0, 0, 119, 29))), ctypes.get_last_error()
        output = ConsoleOutput(stream)

        def position() -> tuple[int, int]:
            info = BufferInfo()
            assert get_info(handle, ctypes.byref(info)), ctypes.get_last_error()
            return info.cursor.x, info.cursor.y

        restore = "\x1b[26;10H"
        frame = (
            "".join(
                f"\x1b[{y + 1};1H" + "".join(f"\x1b[38;2;{x};80;90m⣿" for x in range(114))
                for y in range(20)
            )
            + "\x1b[24;1H汉🙂⣿"
            + restore
        )
        # Negative control: the original transport exposes drawing positions.
        fragmented = set()
        for start in range(0, len(frame), 8192):
            output.write(frame[start : start + 8192])
            fragmented.add(position())
        assert position() == (9, 25)

        native_write = windows_console._WRITE_CONSOLE
        native_writes = 0

        def record_write(handle, data, units, written, reserved):
            nonlocal native_writes
            # This boundary counts UTF-16 units and must include the full emoji
            # surrogate pair, all color sequences, and the final cursor restore.
            assert data[: units * 2].decode("utf-16-le") == frame
            native_writes += 1
            return native_write(handle, data, units, written, reserved)

        windows_console._WRITE_CONSOLE = record_write
        ready, stop = Event(), Event()
        positions = []

        def sample() -> None:
            while not stop.is_set():
                positions.append(position())
                ready.set()

        sampler = Thread(target=sample)
        sampler.start()
        try:
            assert ready.wait(2)
            for _ in range(100):
                assert output.write(frame) == len(frame)
        finally:
            stop.set()
            sampler.join(timeout=2)
            windows_console._WRITE_CONSOLE = native_write
        assert not sampler.is_alive()
        line = ctypes.create_unicode_buffer(114)
        count = wintypes.DWORD()
        assert read(handle, line, 114, Coord(0, 0), ctypes.byref(count))
        assert line[: count.value] == "⣿" * 114
        print(
            json.dumps(
                {
                    "fragmented_positions": len(fragmented),
                    "native_writes": native_writes,
                    "sample_count": len(positions),
                    "positions": sorted(set(positions)),
                }
            )
        )


if __name__ == "__main__":
    main()
