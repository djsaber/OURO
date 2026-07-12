from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from ouro.config.skills import SkillConfigManager, SkillInstallScope
from ouro.core.errors import OuroError
from ouro.prompts import render_agent_skills_prompt
from ouro.skills import (
    LoadedSkill,
    SkillDiagnostic,
    SkillLoader,
    SkillManagementResult,
    SkillManager,
    SkillMetadata,
    default_skill_roots,
    discover_skill_catalog,
)


def list_skills(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
    include_disabled: bool = False,
) -> int:
    try:
        catalog = discover_skill_catalog(home=home, include_disabled=include_disabled)
        skills = catalog.skills
        roots = default_skill_roots(home=home)
        installed = SkillConfigManager(home).snapshot().config.installed
    except OuroError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "roots": [_skill_root_to_dict(root) for root in roots],
                    "skills": [
                        {
                            **_skill_metadata_to_dict(skill),
                            "managed": skill.name in installed,
                            "install": installed[skill.name].model_dump(mode="json")
                            if skill.name in installed
                            else None,
                        }
                        for skill in skills
                    ],
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
        source = f"[{skill.source}]"
        state = "[enabled]" if skill.enabled else "[disabled]"
        managed = "[managed]" if skill.name in installed else "[external]"
        location = f"[file: {skill.location}]"
        lines.append(f"- {source} {state} {managed} {skill.name} {location}")
    print("\n".join(lines))
    _print_diagnostics(catalog.diagnostics)
    return 0


def show_skill(
    name: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        skill = _find_skill(
            discover_skill_catalog(home=home, include_disabled=True).skills,
            name,
        )
    except OuroError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1
    if skill is None:
        _print_skill_not_found(name, json_mode=json_mode)
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
        f"Skill: {loaded.metadata.name}",
        f"Source: {loaded.metadata.source}",
        f"Enabled: {'yes' if loaded.metadata.enabled else 'no'}",
        f"Location: {loaded.metadata.location}",
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
    try:
        roots = default_skill_roots(home=home)
    except OuroError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1
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
    try:
        skills = discover_skill_catalog(home=home).skills
    except OuroError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1
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


def validate_skills(
    path: str | Path | None = None,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        if path is None:
            catalog = discover_skill_catalog(home=home, include_disabled=True)
            skills = catalog.skills
            diagnostics = catalog.diagnostics
            valid = bool(skills) and not any(item.level == "error" for item in diagnostics)
        else:
            result = SkillManager(home).validate(path)
            skills = list(result.skills)
            diagnostics = list(result.diagnostics)
            valid = result.valid
    except OuroError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1

    payload = {
        "status": "ok" if valid else "failed",
        "valid": valid,
        "skills": [_skill_metadata_to_dict(skill) for skill in skills],
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Validated {len(skills)} skill(s): {'valid' if valid else 'failed'}")
        _print_diagnostics(diagnostics)
    return 0 if valid else 1


def install_skills(
    source: str,
    *,
    home: str | Path | None = None,
    scope: SkillInstallScope = "user",
    selector: str | None = None,
    install_all: bool = False,
    force: bool = False,
    ref: str | None = None,
    json_mode: bool = False,
) -> int:
    return _run_management(
        lambda: SkillManager(home).install(
            source,
            scope=scope,
            selector=selector,
            install_all=install_all,
            force=force,
            ref=ref,
        ),
        json_mode=json_mode,
    )


def update_skill(
    name: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    return _run_management(
        lambda: SkillManager(home).update(name),
        json_mode=json_mode,
    )


def remove_skill(
    name: str,
    *,
    home: str | Path | None = None,
    force: bool = False,
    json_mode: bool = False,
) -> int:
    return _run_management(
        lambda: SkillManager(home).remove(name, force=force),
        json_mode=json_mode,
    )


def set_skill_enabled(
    name: str,
    *,
    enabled: bool,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    return _run_management(
        lambda: SkillManager(home).set_enabled(name, enabled=enabled),
        json_mode=json_mode,
    )


def _find_skill(skills: list[SkillMetadata], name: str) -> SkillMetadata | None:
    for skill in skills:
        if skill.name == name:
            return skill
    return None


def _load_skill(skill: SkillMetadata) -> LoadedSkill:
    root = Path(skill.location).parent
    loaded = SkillLoader(root, default_source=skill.source).load_content(skill.name)
    return loaded.model_copy(
        update={"metadata": loaded.metadata.model_copy(update={"enabled": skill.enabled})}
    )


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


def _print_skill_not_found(name: str, *, json_mode: bool) -> None:
    _print_skill_error(f"skill not found: {name}", json_mode=json_mode)


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


def _run_management(
    operation: Callable[[], SkillManagementResult],
    *,
    json_mode: bool,
) -> int:
    try:
        result = operation()
    except (OuroError, OSError, ValueError) as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1
    if json_mode:
        print(json.dumps(_management_result_to_dict(result), ensure_ascii=False))
    else:
        if result.skills:
            for skill in result.skills:
                print(f"Skill {result.action}: {skill.name} -> {skill.target}")
        else:
            print(f"Skill {result.action}.")
        _print_diagnostics(result.diagnostics)
    return 0


def _management_result_to_dict(result: SkillManagementResult) -> dict[str, object]:
    return {
        "status": "ok",
        "action": result.action,
        "skills": [
            {
                "name": skill.name,
                "source": skill.source,
                "target": str(skill.target),
                "scope": skill.scope,
            }
            for skill in result.skills
        ],
        "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
    }
