from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from ouro.core.errors import ConfigError
from ouro.local_locks import FileMutationLock


class JsonConfigFile:
    """Read and atomically replace one of Ouro's JSON configuration files."""

    def __init__(self, root: str | Path | None = None, *, name: str = "config.json") -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.path = self.root / "config" / name
        self._lock_path = self.root / "locks" / f"{Path(name).stem}-mutations.lock"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid config file {self.path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read config file {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"invalid config file {self.path}: expected object")
        return raw

    def write(self, raw: Mapping[str, Any]) -> None:
        with self.mutation():
            self._write_unlocked(raw)

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """Hold the cross-process transaction boundary for one read-modify-write."""

        with FileMutationLock(self._lock_path):
            yield

    def _write_unlocked(self, raw: Mapping[str, Any]) -> None:
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True)
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as temp:
                temp.write(text)
                temp.write("\n")
                temp_path = Path(temp.name)
            temp_path.replace(self.path)
        except OSError as exc:
            raise ConfigError(f"could not write config file {self.path}: {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"could not serialize config file {self.path}: {exc}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()


def atomic_config_update[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    """Run a manager command as one locked read-validate-write transaction."""

    @wraps(operation)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        owner: Any = args[0]
        config_file = getattr(owner, "_file", None)
        if config_file is None:
            config_file = owner.file
        with config_file.mutation():
            return operation(*args, **kwargs)

    return wrapped
