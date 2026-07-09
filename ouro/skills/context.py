from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ouro.skills.loader import SkillLoader, SkillMetadata, SkillSource


@dataclass(frozen=True)
class SkillRoot:
    path: Path
    source: SkillSource


def discover_available_skills(
    *,
    home: str | Path | None = None,
    cwd: str | Path | None = None,
) -> list[SkillMetadata]:
    return available_skills_from_roots(default_skill_roots(home=home, cwd=cwd))


def available_skills_from_roots(roots: list[SkillRoot]) -> list[SkillMetadata]:
    merged: dict[str, SkillMetadata] = {}
    for root in roots:
        loader = SkillLoader(root.path, default_source=root.source)
        for metadata in loader.load_metadata():
            if metadata.enabled:
                merged[metadata.skill_id] = metadata
    return sorted(merged.values(), key=lambda item: item.skill_id)


def default_skill_roots(
    *,
    home: str | Path | None = None,
    cwd: str | Path | None = None,
) -> list[SkillRoot]:
    root_value = home if home is not None else os.environ.get("OURO_HOME")
    ouro_home = Path(root_value) if root_value is not None else Path.home() / ".ouro"
    workspace = Path(cwd) if cwd is not None else Path.cwd()

    candidates = [
        SkillRoot(ouro_home / "skills", "user"),
        SkillRoot(workspace / "skills", "workspace"),
        SkillRoot(workspace / ".ouro" / "skills", "workspace"),
    ]
    roots: list[SkillRoot] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(SkillRoot(resolved, candidate.source))
    return roots
