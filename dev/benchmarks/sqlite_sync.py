"""Observe real SQLite sync calls in an isolated benchmark process.

Only the benchmark process is instrumented. Calls still reach the original OS
function with its return value and errno/last-error intact. No replacement VFS,
SQLite build, application dependency or durability setting is introduced.
See https://www.sqlite.org/c3ref/vfs.html (xGetSystemCall/xSetSystemCall).
"""

from __future__ import annotations

import _sqlite3
import ctypes
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


class _Vfs(ctypes.Structure):
    _fields_ = [
        ("iVersion", ctypes.c_int),
        ("szOsFile", ctypes.c_int),
        ("mxPathname", ctypes.c_int),
        ("pNext", ctypes.c_void_p),
        ("zName", ctypes.c_char_p),
        ("pAppData", ctypes.c_void_p),
        *[
            (name, ctypes.c_void_p)
            for name in (
                "xOpen",
                "xDelete",
                "xAccess",
                "xFullPathname",
                "xDlOpen",
                "xDlError",
                "xDlSym",
                "xDlClose",
                "xRandomness",
                "xSleep",
                "xCurrentTime",
                "xGetLastError",
                "xCurrentTimeInt64",
                "xSetSystemCall",
                "xGetSystemCall",
                "xNextSystemCall",
            )
        ],
    ]


@contextmanager
def sqlite_sync_calls():
    """Yield per-syscall counts; fail explicitly if native observation is unavailable."""
    if sys.platform == "linux":
        with _linux_sync_calls() as counts:
            yield counts
        return
    if sys.platform != "win32":
        raise RuntimeError("Native sync observation supports Windows and Linux")
    module_path = getattr(_sqlite3, "__file__", None)
    if module_path is None:
        raise RuntimeError("Native sync observation requires a shared SQLite library")
    library = ctypes.CDLL(str(Path(module_path).with_name("sqlite3.dll")))
    library.sqlite3_vfs_find.argtypes = [ctypes.c_char_p]
    library.sqlite3_vfs_find.restype = ctypes.POINTER(_Vfs)
    vfs = library.sqlite3_vfs_find(None)
    if not vfs or vfs.contents.iVersion < 3:
        raise RuntimeError("SQLite VFS does not expose syscall observation")
    if not vfs.contents.xGetSystemCall or not vfs.contents.xSetSystemCall:
        raise RuntimeError("SQLite VFS syscall observation is unavailable")
    get_call = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(
        vfs.contents.xGetSystemCall
    )
    set_call = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p)(
        vfs.contents.xSetSystemCall
    )
    signature = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, use_last_error=True)
    name = b"FlushFileBuffers"
    address = get_call(vfs, name)
    if address is None:
        raise RuntimeError("SQLite VFS does not expose FlushFileBuffers")
    original = signature(address)
    counts = {"FlushFileBuffers": 0}

    @signature
    def counted(handle):
        counts["FlushFileBuffers"] += 1
        return original(handle)

    if set_call(vfs, name, ctypes.cast(counted, ctypes.c_void_p)) != 0:
        raise RuntimeError("Cannot observe SQLite FlushFileBuffers")
    try:
        yield counts
    finally:
        set_call(vfs, name, address)


@contextmanager
def _linux_sync_calls():
    # Unix SQLite calls libc directly, outside the VFS syscall override table.
    # Attach/detach and trace-file I/O stay outside the measured interval.
    counts = {"fsync": 0, "fdatasync": 0}
    with tempfile.TemporaryDirectory(prefix="ouro-sync-") as directory:
        trace = Path(directory) / "sync.trace"
        with subprocess.Popen(
            [
                "strace",
                "-f",
                "-e",
                "trace=fsync,fdatasync",
                "-o",
                str(trace),
                "-p",
                str(os.getpid()),
            ],
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            attached = process.stderr.readline()
            if "attached" not in attached:
                raise RuntimeError(f"Cannot observe SQLite sync calls: {attached.strip()}")
            try:
                yield counts
            finally:
                process.terminate()
                process.communicate()
        for name in re.findall(r"\b(fsync|fdatasync)\(", trace.read_text()):
            counts[name] += 1
