from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep

from ouro.core.base import OuroModel
from ouro.core.errors import PersistenceError

_WINDOWS_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32)
_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32})


def write_model_atomic(path: Path, model: OuroModel) -> None:
    write_text_atomic(path, model.model_dump_json(indent=2))


def write_json_atomic(path: Path, value: object) -> None:
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2, default=str))


def write_text_atomic(path: Path, text: str) -> None:
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as tmp:
            tmp.write(text)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = Path(tmp.name)
        _replace_with_retry(temp_path, path)
    except (OSError, TypeError, ValueError) as exc:
        raise PersistenceError(f"could not write {path}: {exc}") from exc
    finally:
        if temp_path is not None and temp_path.exists():
            with suppress(OSError):
                temp_path.unlink()


def _replace_with_retry(source: Path, target: Path) -> None:
    """Atomically replace ``target``, tolerating bounded Windows sharing races."""

    for delay in (*_WINDOWS_REPLACE_RETRY_DELAYS, None):
        try:
            source.replace(target)
            return
        except OSError as exc:
            if delay is None or not _is_transient_windows_replace_error(exc):
                raise
            sleep(delay)


def _is_transient_windows_replace_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in _WINDOWS_TRANSIENT_REPLACE_ERRORS
