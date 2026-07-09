from __future__ import annotations

import json

from ouro.prompts import render_agent_skills_prompt
from ouro.skills import SkillLoader, default_skill_roots, discover_available_skills


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


def test_skill_loader_uses_default_source_for_workspace_skills(tmp_path) -> None:
    skill_dir = tmp_path / "xlsx"
    skill_dir.mkdir()
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "skill_id": "xlsx",
                "name": "Spreadsheet",
                "description": "Process spreadsheets.",
            }
        ),
        encoding="utf-8",
    )

    metadata = SkillLoader(tmp_path, default_source="workspace").load_metadata()

    assert metadata[0].source == "workspace"


def test_discover_available_skills_reads_home_and_workspace_skills(
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    _write_skill(home / "skills" / "pdf", "pdf", "PDF", "Extract text from PDFs.")
    _write_skill(workspace / "skills" / "xlsx", "xlsx", "XLSX", "Process spreadsheets.")
    monkeypatch.chdir(workspace)

    skills = discover_available_skills(home=home)
    prompt = render_agent_skills_prompt(skills)

    assert [skill.skill_id for skill in skills] == ["pdf", "xlsx"]
    assert '"pdf": Extract text from PDFs.' in prompt
    assert '"xlsx": Process spreadsheets.' in prompt
    assert "<skills_instructions>" in prompt
    assert "<available_skills>" in prompt


def test_default_skill_roots_use_overridden_home_and_current_directory(
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path / "custom-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    roots = default_skill_roots(home=home)

    assert roots[0].path == (home / "skills").resolve()
    assert roots[0].source == "user"
    assert (workspace / "skills").resolve() in {root.path for root in roots}


def test_default_skill_roots_follow_ouro_home_env(tmp_path, monkeypatch) -> None:
    home = tmp_path / "env-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OURO_HOME", str(home))

    roots = default_skill_roots(cwd=workspace)

    assert roots[0].path == (home / "skills").resolve()
    assert (workspace / "skills").resolve() in {root.path for root in roots}


def _write_skill(root, skill_id: str, name: str, description: str) -> None:
    root.mkdir(parents=True)
    (root / "skill.json").write_text(
        json.dumps(
            {
                "skill_id": skill_id,
                "name": name,
                "description": description,
            }
        ),
        encoding="utf-8",
    )
    (root / "SKILL.md").write_text(f"{name} instructions.", encoding="utf-8")
