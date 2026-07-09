from __future__ import annotations

import json

from ouro.skills import SkillLoader


def test_skill_loader_loads_metadata_before_full_content(tmp_path) -> None:
    skill_dir = tmp_path / "writer"
    skill_dir.mkdir()
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "skill_id": "writer",
                "name": "Writer",
                "description": "Draft text.",
                "source": "user",
                "triggers": ["write"],
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("Full skill instructions.", encoding="utf-8")

    loader = SkillLoader(tmp_path)
    metadata = loader.load_metadata()

    assert len(metadata) == 1
    assert metadata[0].skill_id == "writer"
    assert not hasattr(metadata[0], "content")

    loaded = loader.load_content("writer")
    assert loaded.metadata.skill_id == "writer"
    assert loaded.content == "Full skill instructions."
