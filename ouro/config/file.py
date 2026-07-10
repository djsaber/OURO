from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from ouro.core.errors import ConfigError


class JsonConfigFile:
    """Read and atomically replace Ouro's shared JSON configuration file."""

    def __init__(self, root: str | Path | None = None) -> None:
        root_value = root if root is not None else os.environ.get("OURO_HOME")
        self.root = Path(root_value) if root_value is not None else Path.home() / ".ouro"
        self.path = self.root / "config" / "config.json"

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
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
