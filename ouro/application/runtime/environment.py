from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """Host data root supplied by the composition root, independent of storage."""

    home: Path | None = None

    @classmethod
    def local(cls, home: str | Path) -> RuntimeEnvironment:
        return cls(home=Path(home).expanduser().resolve())

    @property
    def artifact_root(self) -> Path | None:
        """Return the Host-managed, user-data root for agent-created artifacts."""

        return self.home / "artifacts" if self.home is not None else None
