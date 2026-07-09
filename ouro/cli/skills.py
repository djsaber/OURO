from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.prompts import render_agent_skills_prompt
from ouro.skills import SkillMetadata, default_skill_roots, discover_available_skills
from ouro.skills.loader import LoadedSkill


def list_skills(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    skills = discover_available_skills(home=home)
    roots = default_skill_roots(home=home)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "roots": [_skill_root_to_dict(root) for root in roots],
                    "skills": [_skill_metadata_to_dict(skill) for skill in skills],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not skills:
        print("No skills found.")
        return 0
    lines = ["Skills:"]
    for skill in skills:
        source = f"[{skill.source}]"
        description = f" - {skill.description}" if skill.description else ""
        lines.append(f"- {skill.skill_id} {source} {skill.name}{description}")
    print("\n".join(lines))
    return 0


def show_skill(
    skill_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    skill = _find_skill(discover_available_skills(home=home), skill_id)
    if skill is None:
        _print_skill_not_found(skill_id, json_mode=json_mode)
        return 1

    try:
        loaded = _load_skill(skill)
    except OSError as exc:
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
    if loaded.metadata.triggers:
        lines.append(f"Triggers: {', '.join(loaded.metadata.triggers)}")
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
    skills = discover_available_skills(home=home)
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
        if skill.skill_id == skill_id:
            return skill
    return None


def _load_skill(skill: SkillMetadata) -> LoadedSkill:
    content = (Path(skill.path) / "SKILL.md").read_text(encoding="utf-8")
    return LoadedSkill(metadata=skill, content=content)


def _skill_metadata_to_dict(skill: SkillMetadata) -> dict[str, object]:
    return skill.model_dump(mode="json")


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
