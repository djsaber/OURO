from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ouro.config.skills import SkillConfigManager
from ouro.core.errors import SkillManagementError
from ouro.skills import SkillManager, discover_skill_catalog


def test_install_records_provenance_without_mutating_skill_md(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    source = tmp_path / "community" / "review"
    original = _write_skill(source, "review", "Review code.", "Original instructions.")

    result = SkillManager(home, cwd=workspace).install(str(source))

    target = home / "skills" / "review"
    assert result.action == "installed"
    assert result.skills[0].target == target.resolve()
    assert (target / "SKILL.md").read_text(encoding="utf-8") == original
    assert source.joinpath("SKILL.md").read_text(encoding="utf-8") == original
    record = SkillConfigManager(home).snapshot().config.installed["review"]
    assert record.source == str(source.resolve())
    assert record.target == str(target.resolve())
    assert "source" not in _frontmatter_text(target / "SKILL.md")


def test_install_collection_requires_selection_and_supports_all(tmp_path) -> None:
    source = tmp_path / "collection"
    _write_skill(source / "alpha", "alpha", "Alpha skill.")
    _write_skill(source / "beta", "beta", "Beta skill.")
    manager = SkillManager(tmp_path / "home", cwd=tmp_path / "workspace")

    with pytest.raises(SkillManagementError, match="multiple skills"):
        manager.install(str(source))

    result = manager.install(str(source), install_all=True)

    assert [skill.name for skill in result.skills] == ["alpha", "beta"]
    assert (tmp_path / "home" / "skills" / "alpha" / "SKILL.md").exists()
    assert (tmp_path / "home" / "skills" / "beta" / "SKILL.md").exists()


def test_update_reinstalls_from_recorded_source_and_remove_forgets_record(tmp_path) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source" / "writer"
    _write_skill(source, "writer", "Write text.", "Version one.")
    manager = SkillManager(home, cwd=tmp_path / "workspace")
    manager.install(str(source))
    _write_skill(source, "writer", "Write text.", "Version two.")

    updated = manager.update("writer")

    assert updated.action == "updated"
    target = home / "skills" / "writer" / "SKILL.md"
    assert "Version two." in target.read_text(encoding="utf-8")

    removed = manager.remove("writer")

    assert removed.action == "removed"
    assert not target.parent.exists()
    assert "writer" not in SkillConfigManager(home).snapshot().config.installed


def test_remove_refuses_external_skill_even_with_force(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    external = workspace / ".agents" / "skills" / "external"
    _write_skill(external, "external", "External skill.")
    manager = SkillManager(home, cwd=workspace)

    with pytest.raises(SkillManagementError, match="not managed"):
        manager.remove("external", force=True)

    assert external.exists()


def test_disable_and_enable_are_central_config_overrides(tmp_path) -> None:
    home = tmp_path / "home"
    source = home / "skills" / "pdf"
    _write_skill(source, "pdf", "Work with PDFs.")
    manager = SkillManager(home, cwd=tmp_path / "workspace")

    manager.set_enabled("pdf", enabled=False)

    assert discover_skill_catalog(home=home, cwd=manager.cwd).skills == []
    disabled = discover_skill_catalog(
        home=home,
        cwd=manager.cwd,
        include_disabled=True,
    ).skills
    assert disabled[0].enabled is False
    config = json.loads((home / "config" / "config.json").read_text(encoding="utf-8"))
    assert config["skills"]["disabled"] == ["pdf"]

    manager.set_enabled("pdf", enabled=True)

    assert discover_skill_catalog(home=home, cwd=manager.cwd).skills[0].enabled is True


def test_validate_reports_invalid_skill_without_installing_it(tmp_path) -> None:
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "SKILL.md").write_text(
        """---
name: invalid
---
Missing description.
""",
        encoding="utf-8",
    )

    result = SkillManager(tmp_path / "home").validate(invalid)

    assert result.valid is False
    assert any(item.code == "missing_description" for item in result.diagnostics)


def test_install_from_git_url_with_skill_subdirectory(tmp_path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    repository = tmp_path / "repository"
    skill = repository / "skills" / "git-skill"
    _write_skill(skill, "git-skill", "Installed from git.")
    subprocess.run([git, "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        [git, "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        [git, "-C", str(repository), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run([git, "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [git, "-C", str(repository), "commit", "-m", "skill"],
        check=True,
        capture_output=True,
    )
    source = f"{repository.as_uri()}#skills/git-skill"

    result = SkillManager(tmp_path / "home").install(source)

    assert result.skills[0].name == "git-skill"
    assert (tmp_path / "home" / "skills" / "git-skill" / "SKILL.md").exists()
    assert (
        SkillConfigManager(tmp_path / "home").snapshot().config.installed["git-skill"].source
        == source
    )


def _write_skill(
    root: Path,
    name: str,
    description: str,
    body: str = "Instructions.",
) -> str:
    root.mkdir(parents=True, exist_ok=True)
    content = f"""---
name: {name}
description: {description}
---
{body}
"""
    (root / "SKILL.md").write_text(content, encoding="utf-8")
    return content


def _frontmatter_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("---", 2)[1]
