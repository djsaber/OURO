from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from ouro.core.errors import ConfigError, PersistenceError
from ouro.local_io import read_text_atomic, write_text_atomic
from ouro.local_locks import FileMutationLock

DOMAIN_CONFIG_FILES = {
    "providers": "providers.json",
    "mcp": "mcp.json",
    "agents": "agents.json",
    "prompts": "prompts.json",
    "system_prompt": "prompts.json",
}


class ConfigMutationTimeout(ConfigError):
    """The bounded configuration transaction could not acquire its lock."""


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
            raw = json.loads(read_text_atomic(self.path))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid config file {self.path}: {exc}") from exc
        except UnicodeError as exc:
            raise ConfigError(f"config file is not valid UTF-8: {self.path}") from exc
        except OSError as exc:
            raise ConfigError(f"could not read config file {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"invalid config file {self.path}: expected object")
        if self.path.name == "config.json":
            obsolete = sorted(raw.keys() & DOMAIN_CONFIG_FILES.keys())
            if obsolete:
                locations = ", ".join(DOMAIN_CONFIG_FILES[key] for key in obsolete)
                raise ConfigError(
                    f"unsupported shared config sections: {', '.join(obsolete)}; "
                    f"use {locations} in a new home or remove the obsolete configuration"
                )
        return raw

    def write(self, raw: Mapping[str, Any]) -> None:
        with self.mutation():
            self._write_unlocked(raw)

    @contextmanager
    def mutation(
        self, *, timeout: float = 10.0, cancel_event: threading.Event | None = None
    ) -> Iterator[None]:
        """Bound acquisition, then finish one synchronous read-modify-write.

        Async callers must run the entire transaction in one worker, never hold
        this thread-reentrant lock across an await. Cancelling acquisition does
        not roll back a transaction that has already started.
        """
        lock = FileMutationLock(self._lock_path)
        if not lock.acquire(timeout=timeout, cancel_event=cancel_event):
            raise ConfigMutationTimeout("timed out waiting for configuration mutation")
        try:
            yield
        finally:
            lock.release()

    def _write_unlocked(self, raw: Mapping[str, Any]) -> None:
        try:
            text = json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"could not serialize config file {self.path}: {exc}") from exc
        try:
            write_text_atomic(self.path, text + "\n")
        except PersistenceError as exc:
            raise ConfigError(f"could not write config file {self.path}: {exc}") from exc


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
