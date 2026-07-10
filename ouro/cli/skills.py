from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ouro.prompts import render_agent_skills_prompt
from ouro.skills import (
    LoadedSkill,
    SkillDiagnostic,
    SkillLoader,
    SkillMetadata,
    default_skill_roots,
    discover_skill_catalog,
)


def list_skills(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    catalog = discover_skill_catalog(home=home)
    skills = catalog.skills
    roots = default_skill_roots(home=home)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "roots": [_skill_root_to_dict(root) for root in roots],
                    "skills": [_skill_metadata_to_dict(skill) for skill in skills],
                    "diagnostics": [item.model_dump(mode="json") for item in catalog.diagnostics],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not skills:
        print("No skills found.")
        _print_diagnostics(catalog.diagnostics)
        return 0
    lines = ["Skills:"]
    for skill in skills:
        skill_id = f"[id: {skill.skill_id}]"
        source = f"[{skill.source}]"
        location = f"[file: {skill.location}]"
        lines.append(f"- {source} {skill.name} {skill_id} {location}")
    print("\n".join(lines))
    _print_diagnostics(catalog.diagnostics)
    return 0


def show_skill(
    skill_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    skill = _find_skill(discover_skill_catalog(home=home).skills, skill_id)
    if skill is None:
        _print_skill_not_found(skill_id, json_mode=json_mode)
        return 1

    try:
        loaded = _load_skill(skill)
    except (OSError, ValueError) as exc:
        _print_skill_error(f"failed to load skill content: {exc}", json_mode=json_mode)
        return 1

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "skill": _loaded_skill_to_dict(loaded),
                },
                ensure_ascii=False,
            )
        )
        return 0

    lines = [
        f"Skill: {loaded.metadata.skill_id}",
        f"Name: {loaded.metadata.name}",
        f"Source: {loaded.metadata.source}",
        f"Enabled: {'yes' if loaded.metadata.enabled else 'no'}",
        f"Path: {loaded.metadata.path}",
    ]
    if loaded.metadata.description:
        lines.append(f"Description: {loaded.metadata.description}")
    if loaded.metadata.compatibility:
        lines.append(f"Compatibility: {loaded.metadata.compatibility}")
    if loaded.metadata.allowed_tools:
        lines.append(f"Allowed tools: {loaded.metadata.allowed_tools}")
    if loaded.resources:
        lines.append(f"Resources: {', '.join(loaded.resources)}")
    lines.extend(["", loaded.content])
    print("\n".join(lines))
    return 0


def show_skill_roots(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    roots = default_skill_roots(home=home)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "roots": [_skill_root_to_dict(root) for root in roots],
                },
                ensure_ascii=False,
            )
        )
        return 0

    lines = ["Skill roots:"]
    for root in roots:
        exists = "exists" if root.path.exists() else "missing"
        lines.append(f"- {root.source}: {root.path} ({exists})")
    print("\n".join(lines))
    return 0


def show_skills_prompt(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    skills = discover_skill_catalog(home=home).skills
    prompt = render_agent_skills_prompt(skills)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "skill_count": len(skills),
                    "prompt": prompt,
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(prompt or "No skills prompt: no skills found.")
    return 0


def _find_skill(skills: list[SkillMetadata], skill_id: str) -> SkillMetadata | None:
    for skill in skills:
        if skill.name == skill_id:
            return skill
    return None


def _load_skill(skill: SkillMetadata) -> LoadedSkill:
    return SkillLoader(skill.path, default_source=skill.source).load_content(skill.name)


def _skill_metadata_to_dict(skill: SkillMetadata) -> dict[str, object]:
    return {
        **skill.model_dump(mode="json"),
        "skill_id": skill.name,
        "path": skill.path,
    }


def _loaded_skill_to_dict(loaded: LoadedSkill) -> dict[str, object]:
    return {
        **_skill_metadata_to_dict(loaded.metadata),
        "content": loaded.content,
    }


def _skill_root_to_dict(root) -> dict[str, str | bool]:
    return {
        "path": str(root.path),
        "source": root.source,
        "exists": root.path.exists(),
    }


def _print_skill_not_found(skill_id: str, *, json_mode: bool) -> None:
    _print_skill_error(f"skill not found: {skill_id}", json_mode=json_mode)


def _print_skill_error(message: str, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False))
    else:
        print(f"Error: {message}", file=sys.stderr)


def _print_diagnostics(diagnostics: Sequence[SkillDiagnostic]) -> None:
    for item in diagnostics:
        print(
            f"{item.level.title()}: [{item.code}] {item.message} ({item.path})",
            file=sys.stderr,
        )
