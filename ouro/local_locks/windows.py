"""Windows shared/exclusive byte-range locks for local ownership files."""

from __future__ import annotations

import ctypes
import msvcrt
from ctypes import wintypes


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


_API = ctypes.WinDLL("kernel32", use_last_error=True)
_API.LockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(_Overlapped),
]
_API.LockFileEx.restype = wintypes.BOOL
_API.UnlockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(_Overlapped),
]
_API.UnlockFileEx.restype = wintypes.BOOL


def try_lock(descriptor: int, *, exclusive: bool) -> bool:
    # LOCKFILE_FAIL_IMMEDIATELY; no pending OVERLAPPED operation survives this call.
    flags = 1 | (2 if exclusive else 0)
    if _API.LockFileEx(
        msvcrt.get_osfhandle(descriptor), flags, 0, 1, 0, ctypes.byref(_Overlapped())
    ):
        return True
    error = ctypes.get_last_error()
    if error == 33:  # ERROR_LOCK_VIOLATION
        return False
    raise ctypes.WinError(error)


def unlock(descriptor: int) -> None:
    if not _API.UnlockFileEx(
        msvcrt.get_osfhandle(descriptor), 0, 1, 0, ctypes.byref(_Overlapped())
    ):
        raise ctypes.WinError(ctypes.get_last_error())
