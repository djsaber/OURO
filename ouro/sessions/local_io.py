from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

from ouro.core.base import OuroModel
from ouro.core.errors import PersistenceError


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
        temp_path.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        raise PersistenceError(f"could not write {path}: {exc}") from exc
    finally:
        if temp_path is not None and temp_path.exists():
            with suppress(OSError):
                temp_path.unlink()
