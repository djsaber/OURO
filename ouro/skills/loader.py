from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, OuroModel

SkillSource = Literal["user", "workspace", "plugin"]


class SkillMetadata(OuroModel):
    skill_id: str
    name: str
    description: str = ""
    source: SkillSource = "user"
    path: str
    triggers: list[str] = Field(default_factory=list)
    enabled: bool = True
    metadata: Metadata = MetadataField


class LoadedSkill(OuroModel):
    metadata: SkillMetadata
    content: str


class SkillLoader:
    def __init__(self, root: str | Path, *, default_source: SkillSource = "user") -> None:
        self.root = Path(root)
        self.default_source = default_source
        self._metadata: dict[str, SkillMetadata] = {}

    def load_metadata(self) -> list[SkillMetadata]:
        self._metadata = {}
        if not self.root.exists():
            return []
        for manifest in self._metadata_paths():
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            metadata = SkillMetadata.model_validate(
                {
                    **raw,
                    "source": raw.get("source") or self.default_source,
                    "path": str(manifest.parent),
                }
            )
            self._metadata[metadata.skill_id] = metadata
        return sorted(self._metadata.values(), key=lambda item: item.skill_id)

    def load_content(self, skill_id: str) -> LoadedSkill:
        metadata = self._metadata.get(skill_id)
        if metadata is None:
            for item in self.load_metadata():
                if item.skill_id == skill_id:
                    metadata = item
                    break
        if metadata is None:
            raise KeyError(f"unknown skill: {skill_id}")
        content_path = Path(metadata.path) / "SKILL.md"
        return LoadedSkill(
            metadata=metadata,
            content=content_path.read_text(encoding="utf-8"),
        )

    def _metadata_paths(self) -> list[Path]:
        direct = self.root / "skill.json"
        paths: list[Path] = []
        if direct.exists():
            paths.append(direct)
        for child in self.root.iterdir():
            manifest = child / "skill.json"
            if child.is_dir() and manifest.exists():
                paths.append(manifest)
        return paths
