from __future__ import annotations

import json

import pytest

from ouro.core.errors import ConfigError
from ouro.prompts import render_agent_skills_prompt
from ouro.skills import (
    SkillLoader,
    SkillRoot,
    default_skill_roots,
    discover_available_skills,
    discover_skill_catalog,
    skill_catalog_from_roots,
)


def test_loader_uses_skill_md_as_the_manifest_and_loads_body_on_activation(tmp_path) -> None:
    skill_dir = tmp_path / "writer"
    _write_skill(
        skill_dir,
        "writer",
        "Draft and revise text. Use when the user asks for writing help.",
        body="# Writer\n\nFull skill instructions.",
        extra="""
license: Apache-2.0
compatibility: Requires a text editor
metadata:
  author: community
  version: "1.2"
allowed-tools: Read Write
user-invocable: false
argument-hint: "[topic]"
x-community-field: preserved
""",
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "STYLE.md").write_text("Style guide", encoding="utf-8")

    loader = SkillLoader(tmp_path)
    metadata = loader.load_metadata()

    assert len(metadata) == 1
    assert metadata[0].name == "writer"
    assert metadata[0].license == "Apache-2.0"
    assert metadata[0].metadata == {"author": "community", "version": "1.2"}
    assert metadata[0].allowed_tools == "Read Write"
    assert metadata[0].user_invocable is False
    assert metadata[0].argument_hint == "[topic]"
    assert metadata[0].extensions == {"x-community-field": "preserved"}
    assert not hasattr(metadata[0], "content")

    loaded = loader.load_content("writer")
    assert loaded.content == "# Writer\n\nFull skill instructions."
    assert loaded.resources == ["references/STYLE.md"]


def test_loader_rejects_malformed_community_format_variants(tmp_path) -> None:
    skill_dir = tmp_path / "community-folder"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: Community Skill
description: Use this skill when: the user asks for community help
allowed-tools:
  - Read
  - Bash
---

Community instructions.
""",
        encoding="utf-8",
    )

    catalog = SkillLoader(tmp_path).load_catalog()

    assert catalog.skills == []
    codes = {item.code for item in catalog.diagnostics}
    assert "invalid_yaml" in codes


def test_loader_requires_name_and_description(tmp_path) -> None:
    _write_raw_skill(
        tmp_path / "missing-name",
        """---
description: Still discoverable.
---
Instructions.
""",
    )
    _write_raw_skill(
        tmp_path / "missing-description",
        """---
name: missing-description
---
Instructions.
""",
    )

    catalog = SkillLoader(tmp_path).load_catalog()

    assert catalog.skills == []
    assert {item.code for item in catalog.diagnostics} >= {
        "missing_name",
        "missing_description",
    }


def test_loader_recurses_with_bounds_and_ignores_skill_json_as_a_manifest(tmp_path) -> None:
    nested = tmp_path / "pack" / "nested-skill"
    _write_skill(nested, "nested-skill", "Use for nested tasks.")
    nonstandard = tmp_path / "nonstandard-manifest"
    nonstandard.mkdir()
    (nonstandard / "skill.json").write_text(
        json.dumps({"name": "nonstandard-manifest", "description": "Ignored format"}),
        encoding="utf-8",
    )
    ignored = tmp_path / "node_modules" / "ignored"
    _write_skill(ignored, "ignored", "Should not load.")

    catalog = SkillLoader(tmp_path).load_catalog()

    assert [skill.name for skill in catalog.skills] == ["nested-skill"]


def test_workspace_skill_deterministically_overrides_user_skill(tmp_path) -> None:
    user_root = tmp_path / "user"
    workspace_root = tmp_path / "workspace"
    _write_skill(user_root / "review", "review", "User review instructions.")
    _write_skill(workspace_root / "review", "review", "Workspace review instructions.")

    catalog = skill_catalog_from_roots(
        [
            SkillRoot(user_root, "user"),
            SkillRoot(workspace_root, "workspace"),
        ]
    )

    assert catalog.skills[0].description == "Workspace review instructions."
    assert catalog.skills[0].source == "workspace"
    assert any(item.code == "shadowed_skill" for item in catalog.diagnostics)


def test_central_config_controls_paths_and_disabled_skills(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = home / "shared-skills"
    _write_skill(home / "skills" / "disabled-skill", "disabled-skill", "Disabled.")
    _write_skill(external / "shared", "shared", "Shared skill.")
    config_dir = home / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "skills": {
                    "paths": ["shared-skills"],
                    "disabled": ["disabled-skill"],
                    "disabled_sources": ["claude.user", "claude.workspace"],
                }
            }
        ),
        encoding="utf-8",
    )

    skills = discover_available_skills(home=home, cwd=workspace)

    assert [skill.name for skill in skills] == ["shared"]


def test_scoped_sources_distinguish_ouro_from_other_hosts(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(home / "skills" / "ouro-skill", "ouro-skill", "OURO guidance.")
    _write_skill(home / ".codex" / "skills" / "codex-skill", "codex-skill", "Codex guidance.")
    _write_skill(
        workspace / ".codex" / "skills" / "workspace-skill",
        "workspace-skill",
        "Workspace Codex guidance.",
    )
    config_dir = home / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"skills": {"disabled_sources": ["ouro.user", "codex.workspace"]}}),
        encoding="utf-8",
    )

    catalog = discover_skill_catalog(home=home, cwd=workspace)
    roots = default_skill_roots(home=home, cwd=workspace)

    assert [(skill.name, skill.origin, skill.source) for skill in catalog.skills] == [
        ("codex-skill", "codex", "user")
    ]
    states = {root.source_id: root.enabled for root in roots}
    assert states["ouro.user"] is False
    assert states["ouro.workspace"] is True
    assert states["codex.user"] is True
    assert states["codex.workspace"] is False


def test_configured_skill_paths_can_be_disabled_individually(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(home / "shared-skills" / "shared", "shared", "Shared guidance.")
    config_dir = home / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "skills": {
                    "paths": [
                        {"path": "shared-skills", "source": "user", "enabled": False}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert discover_skill_catalog(home=home, cwd=workspace).skills == []
    configured = next(
        root
        for root in default_skill_roots(home=home, cwd=workspace)
        if root.origin == "configured"
    )
    assert configured.enabled is False


def test_removed_coarse_scan_fields_fail_with_migration_guidance(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"skills": {"scan_codex": False}}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="use disabled_sources"):
        discover_skill_catalog(home=tmp_path)


def test_discovery_scans_cross_client_locations_and_renders_standard_catalog(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_skill(home / ".agents" / "skills" / "pdf", "pdf", "Extract PDF text.")
    _write_skill(
        workspace / ".claude" / "skills" / "xlsx",
        "xlsx",
        "Process spreadsheets.",
    )
    _write_skill(
        workspace / ".github" / "skills" / "manual-only",
        "manual-only",
        "Only use when explicitly requested.",
        extra="disable-model-invocation: true",
    )

    catalog = discover_skill_catalog(home=home, cwd=workspace)
    prompt = render_agent_skills_prompt(catalog.skills)

    assert [skill.name for skill in catalog.skills] == ["manual-only", "pdf", "xlsx"]
    assert "<name>pdf</name>" in prompt
    assert "<description>Extract PDF text.</description>" in prompt
    assert "<location>" in prompt
    assert "read the full SKILL.md" in prompt
    assert "Invoke skills using this tool" not in prompt
    assert "manual-only" not in prompt


def test_default_roots_include_native_and_community_conventions(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    roots = default_skill_roots(home=home, cwd=workspace)
    paths = {root.path for root in roots}

    assert (home / "skills").resolve() in paths
    assert (home / ".agents" / "skills").resolve() in paths
    assert (home / ".claude" / "skills").resolve() in paths
    assert (home / ".copilot" / "skills").resolve() in paths
    assert (home / ".codex" / "skills").resolve() in paths
    assert (workspace / ".agents" / "skills").resolve() in paths
    assert (workspace / ".claude" / "skills").resolve() in paths
    assert (workspace / ".github" / "skills").resolve() in paths
    assert (workspace / ".codex" / "skills").resolve() in paths
    assert (workspace / ".ouro" / "skills").resolve() in paths
    assert {root.source_id for root in roots} == {
        "agents.user",
        "claude.user",
        "copilot.user",
        "codex.user",
        "ouro.user",
        "agents.workspace",
        "claude.workspace",
        "github.workspace",
        "codex.workspace",
        "ouro.workspace",
    }
    assert all(root.enabled for root in roots)


def test_default_skill_roots_follow_ouro_home_env(tmp_path, monkeypatch) -> None:
    home = tmp_path / "env-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("OURO_HOME", str(home))

    roots = default_skill_roots(cwd=workspace, user_home=tmp_path / "profile")

    assert (home / "skills").resolve() in {root.path for root in roots}


def _write_skill(
    root,
    name: str,
    description: str,
    *,
    body: str = "Skill instructions.",
    extra: str = "",
) -> None:
    _write_raw_skill(
        root,
        f"""---
name: {name}
description: {description}
{extra.rstrip()}
---
{body}
""",
    )


def _write_raw_skill(root, content: str) -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(content, encoding="utf-8")
