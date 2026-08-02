"""Discover reusable Agent instructions for one request workspace."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

InstructionScope = Literal["user", "project"]

DEFAULT_MAX_INSTRUCTION_BYTES = 32 * 1024
INSTRUCTION_FILE_NAME = "AGENTS.md"


class AgentInstructionError(ValueError):
    """A discovered instruction document could not be loaded safely."""


@dataclass(frozen=True, slots=True)
class AgentInstructionDocument:
    path: Path
    scope: InstructionScope
    content: str


class AgentInstructionLoader:
    """Load canonical ``AGENTS.md`` files from broad to specific scope."""

    def __init__(
        self,
        *,
        ouro_home: str | Path | None = None,
        user_home: str | Path | None = None,
        max_bytes: int = DEFAULT_MAX_INSTRUCTION_BYTES,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.ouro_home = _resolve_ouro_home(ouro_home)
        self.user_home = _resolve_user_home(
            ouro_home=self.ouro_home,
            user_home=user_home,
        )
        self.max_bytes = max_bytes

    def load(self, workspace: str | Path | None) -> tuple[AgentInstructionDocument, ...]:
        candidates: list[tuple[Path, InstructionScope]] = [
            *((path, "user") for path in self._user_candidates()),
            *((path, "project") for path in self._project_candidates(workspace)),
        ]
        documents: list[AgentInstructionDocument] = []
        loaded_paths: set[Path] = set()
        loaded_bytes = 0
        for path, scope in candidates:
            if not _is_regular_file(path):
                continue
            canonical = _canonical_path(path)
            if canonical in loaded_paths:
                continue
            content, size = _read_document(
                path,
                remaining_bytes=self.max_bytes - loaded_bytes,
                total_limit=self.max_bytes,
            )
            if not content:
                continue
            loaded_paths.add(canonical)
            loaded_bytes += size
            documents.append(
                AgentInstructionDocument(
                    path=canonical,
                    scope=scope,
                    content=content,
                )
            )
        return tuple(documents)

    def _user_candidates(self) -> tuple[Path, ...]:
        return (
            self.user_home / ".agents" / INSTRUCTION_FILE_NAME,
            self.ouro_home / INSTRUCTION_FILE_NAME,
        )

    def _project_candidates(self, workspace: str | Path | None) -> tuple[Path, ...]:
        if workspace is None:
            return ()
        directory = Path(workspace).expanduser().resolve(strict=False)
        if not directory.is_dir():
            return ()
        project_root = _find_project_root(directory)
        chain: list[Path] = []
        current = directory
        while True:
            chain.append(current)
            if current == project_root:
                break
            current = current.parent
        return tuple(path / INSTRUCTION_FILE_NAME for path in reversed(chain))


def render_agent_instructions(
    documents: tuple[AgentInstructionDocument, ...],
) -> str:
    sections = [
        "AGENTS.md documents contain reusable instructions from the user. They are additive to "
        "the system prompt. Project instructions apply to the directory tree rooted at their "
        "source directory; when instructions conflict, the later and more specific source takes "
        "precedence. Before working below the current workspace, check for a more specific "
        "AGENTS.md in the target file's ancestor directories."
    ]
    for document in documents:
        sections.append(
            f"Instructions from: {document.path}\nScope: {document.scope}\n\n{document.content}"
        )
    return "\n\n--- agent-instructions-source ---\n\n".join(sections)


def _resolve_ouro_home(value: str | Path | None) -> Path:
    configured = value if value is not None else os.environ.get("OURO_HOME")
    root = Path(configured) if configured is not None else Path.home() / ".ouro"
    return root.expanduser().resolve()


def _resolve_user_home(
    *,
    ouro_home: Path,
    user_home: str | Path | None,
) -> Path:
    if user_home is not None:
        return Path(user_home).expanduser().resolve()
    default_ouro_home = (Path.home() / ".ouro").resolve()
    if ouro_home == default_ouro_home:
        return Path.home().resolve()
    return ouro_home


def _find_project_root(workspace: Path) -> Path:
    for directory in (workspace, *workspace.parents):
        if (directory / ".git").exists():
            return directory
    return workspace


def _canonical_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AgentInstructionError(
            f"could not resolve instruction document {path}: {exc}"
        ) from exc


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AgentInstructionError(
            f"could not inspect instruction document {path}: {exc}"
        ) from exc


def _read_document(
    path: Path,
    *,
    remaining_bytes: int,
    total_limit: int,
) -> tuple[str, int]:
    try:
        with path.open("rb") as stream:
            payload = stream.read(remaining_bytes + 1)
    except OSError as exc:
        raise AgentInstructionError(f"could not read instruction document {path}: {exc}") from exc
    if len(payload) > remaining_bytes:
        raise AgentInstructionError(
            f"combined AGENTS.md content exceeds {total_limit} bytes while loading {path}"
        )
    try:
        content = payload.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise AgentInstructionError(f"instruction document must be UTF-8: {path}: {exc}") from exc
    return content, len(payload)


__all__ = [
    "AgentInstructionDocument",
    "AgentInstructionError",
    "AgentInstructionLoader",
    "DEFAULT_MAX_INSTRUCTION_BYTES",
    "INSTRUCTION_FILE_NAME",
    "render_agent_instructions",
]
