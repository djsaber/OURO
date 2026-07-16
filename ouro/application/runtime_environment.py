from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """Deployment metadata supplied by the composition root.

    These paths describe the local deployment, not the Session persistence
    contract. Non-file backends may therefore omit ``session_data_dir``.
    """

    home: Path | None = None
    session_data_dir: Path | None = None

    @classmethod
    def local(
        cls,
        home: str | Path,
        session_data_dir: str | Path | None = None,
    ) -> RuntimeEnvironment:
        return cls(
            home=Path(home).expanduser().resolve(),
            session_data_dir=(
                Path(session_data_dir).expanduser().resolve()
                if session_data_dir is not None
                else None
            ),
        )

    def session_location(self, session_id: str) -> Path | None:
        if self.session_data_dir is None:
            return None
        return self.session_data_dir / session_id
