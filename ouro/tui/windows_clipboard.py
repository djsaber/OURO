"""Read native Windows clipboard file lists without taking ownership of their memory."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_CF_HDROP = 15


def read_file_paths() -> tuple[str, ...]:
    """Copy CF_HDROP paths while the clipboard handle is valid."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    if not user32.IsClipboardFormatAvailable(_CF_HDROP):
        return ()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    query_file = shell32.DragQueryFileW
    query_file.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    query_file.restype = wintypes.UINT
    if not user32.OpenClipboard(None):
        raise ctypes.WinError()
    try:
        handle = user32.GetClipboardData(_CF_HDROP)
        if not handle:
            raise ctypes.WinError()
        paths = []
        for index in range(query_file(handle, 0xFFFFFFFF, None, 0)):
            length = query_file(handle, index, None, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            if not length or query_file(handle, index, buffer, len(buffer)) != length:
                raise OSError("Could not read a file path from the clipboard")
            paths.append(buffer.value)
        return tuple(paths)
    finally:
        if not user32.CloseClipboard():
            raise ctypes.WinError()
