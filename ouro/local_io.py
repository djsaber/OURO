from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep

from ouro.core.base import OuroModel
from ouro.core.errors import PersistenceError

_WINDOWS_TRANSIENT_IO_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32)
_WINDOWS_TRANSIENT_SHARING_ERRORS = frozenset({5, 32})


def write_model_atomic(path: Path, model: OuroModel) -> None:
    write_text_atomic(path, model.model_dump_json(indent=2) + "\n")


def write_json_atomic(path: Path, value: object) -> None:
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    """Replace UTF-8 content exactly, including journal line boundaries."""

    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            delete=False,
        ) as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)
        replace_path_with_retry(temp_path, path)
    except (OSError, TypeError, ValueError) as exc:
        raise PersistenceError(f"could not write {path}: {exc}") from exc
    finally:
        if temp_path is not None and temp_path.exists():
            with suppress(OSError):
                temp_path.unlink()


def read_text_atomic(path: Path) -> str:
    """Read one atomically replaced UTF-8 file, tolerating bounded Windows sharing races."""

    for delay in (*_WINDOWS_TRANSIENT_IO_DELAYS, None):
        try:
            return path.read_text("utf-8")
        except OSError as exc:
            if delay is None or not _is_transient_windows_sharing_error(exc):
                raise
            sleep(delay)
    raise AssertionError("unreachable")


def replace_path_with_retry(source: Path, target: Path) -> None:
    """Atomically replace ``target``, tolerating bounded Windows sharing races."""

    for delay in (*_WINDOWS_TRANSIENT_IO_DELAYS, None):
        try:
            source.replace(target)
            return
        except OSError as exc:
            if delay is None or not _is_transient_windows_sharing_error(exc):
                raise
            sleep(delay)


def _is_transient_windows_sharing_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _WINDOWS_TRANSIENT_SHARING_ERRORS
    return isinstance(exc, PermissionError)
