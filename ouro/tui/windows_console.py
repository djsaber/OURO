"""Write Unicode terminal frames without splitting native console transactions."""

from __future__ import annotations

import ctypes
import msvcrt
from ctypes import wintypes
from typing import TextIO

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_GET_CONSOLE_MODE = _KERNEL32.GetConsoleMode
_GET_CONSOLE_MODE.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_GET_CONSOLE_MODE.restype = wintypes.BOOL
_WRITE_CONSOLE = _KERNEL32.WriteConsoleW
_WRITE_CONSOLE.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_WRITE_CONSOLE.restype = wintypes.BOOL


class ConsoleOutput:
    """Borrow an output stream; use its console handle without owning or closing it."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        handle = msvcrt.get_osfhandle(stream.fileno())
        self._handle = handle if _GET_CONSOLE_MODE(handle, wintypes.DWORD()) else None

    def fileno(self) -> int:
        return self._stream.fileno()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def write(self, text: str) -> int:
        if self._handle is None:
            return self._stream.write(text)
        if not text:
            return 0
        # Python's _WindowsConsoleIO splits large writes internally. Native IME
        # clients can observe the drawing cursor between those calls. Send the
        # whole frame, including its final cursor restoration, in one Win32 call.
        # WriteConsoleW counts UTF-16 units, not Python Unicode code points.
        encoded = text.encode("utf-16-le")
        units = len(encoded) // 2
        written = wintypes.DWORD()
        if not _WRITE_CONSOLE(self._handle, encoded, units, ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != units:
            raise OSError(
                f"Incomplete console frame: wrote {written.value} of {units} UTF-16 units"
            )
        return len(text)

    def flush(self) -> None:
        self._stream.flush()
