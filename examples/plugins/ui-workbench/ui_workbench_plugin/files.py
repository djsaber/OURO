"""Workspace inputs and publication of immutable, user-visible artifact bundles."""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from tempfile import mkdtemp
from typing import BinaryIO


class WorkbenchError(ValueError):
    """An expected, user-facing Workbench failure."""


def workspace_file(workspace: Path, relative: str, *, base: Path | None = None) -> Path:
    """Resolve a regular, visible input without leaving the current workspace."""
    if not isinstance(relative, str) or not relative.strip() or len(relative) > 1024:
        raise WorkbenchError("Provide a nonempty, workspace-relative file path.")
    portable = relative.replace("\\", "/")
    if (
        portable.startswith("/")
        or PureWindowsPath(portable).drive
        or ":" in portable
        or any(ord(char) < 32 for char in portable)
        or _hidden(portable.split("/"))
    ):
        raise WorkbenchError("Use a visible file inside the current workspace.")
    try:
        root = workspace.resolve(strict=True)
        path = ((base if base is not None else root) / portable).resolve(strict=True)
        if not path.is_relative_to(root) or _hidden(path.relative_to(root).parts):
            raise WorkbenchError("Use a visible file inside the current workspace.")
        if not path.is_file():
            raise WorkbenchError("Expected a regular file inside the current workspace.")
    except OSError, RuntimeError:
        raise WorkbenchError("The requested workspace file could not be found or read.") from None
    return path


def _hidden(parts: tuple[str, ...] | list[str]) -> bool:
    return any(part.startswith(".") and part not in {".", ".."} for part in parts)


def read_limited(path: Path, limit: int) -> bytes:
    try:
        with _open_regular(path) as stream:
            content = stream.read(limit + 1)
    except OSError:
        raise WorkbenchError("Could not read the requested file.") from None
    if len(content) > limit:
        raise WorkbenchError(f"An input exceeds the {limit:,}-byte size limit.")
    return content


def _open_regular(path: Path) -> BinaryIO:
    # Non-blocking open avoids hanging on FIFOs if an input changed since resolution.
    def open_input(name: str, flags: int) -> int:
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise WorkbenchError("Expected a regular file.")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    return open(path, "rb", opener=open_input)


_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_RESERVED_STEM = re.compile(r"(?:aux|con|nul|prn|(?:com|lpt)[0-9])", re.IGNORECASE)


def publish_bundle(root: Path, label: str, files: Mapping[str, bytes]) -> Path:
    """Publish a complete bundle without overwriting a previous result."""
    if (
        not _ARTIFACT_NAME.fullmatch(label)
        or not files
        or len(files) > 32
        or any(
            not _ARTIFACT_NAME.fullmatch(name)
            or name.endswith(".")
            or _RESERVED_STEM.fullmatch(name.partition(".")[0]) is not None
            for name in files
        )
        or len({name.lower() for name in files}) != len(files)
    ):
        raise WorkbenchError("Invalid artifact bundle.")
    if sum(map(len, files.values())) > 64 * 1024 * 1024:
        raise WorkbenchError("The artifact bundle exceeds 64 MiB.")
    staging: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(mkdtemp(prefix=f".{label}-", dir=root))
        for name, data in files.items():
            with (staging / name).open("xb") as stream:
                stream.write(data)
        return staging.rename(root / staging.name[1:])
    except OSError:
        raise WorkbenchError("Could not publish the artifact bundle to OURO Home.") from None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
