from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import (
    LoadedSkillView,
    SkillDiagnosticView,
    SkillManagementView,
    SkillRootView,
    SkillView,
)
from ouro.config.skills import SkillInstallScope
from ouro.core.errors import ConfigError, OuroError


def list_skills(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
    include_disabled: bool = False,
) -> int:
    try:
        catalog = ConfigurationFacade(home).skill_catalog(include_disabled=include_disabled)
    except OuroError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "roots": [_skill_root_to_dict(root) for root in catalog.roots],
                    "skills": [
                        {
                            **_skill_metadata_to_dict(skill),
                            "managed": skill.managed,
                            "install": (
                                skill.install.model_dump(mode="json", exclude={"name"})
                                if skill.install is not None
                                else None
                            ),
                        }
                        for skill in catalog.skills
                    ],
                    "diagnostics": [item.model_dump(mode="json") for item in catalog.diagnostics],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not catalog.skills:
        print("No skills found.")
        _print_diagnostics(catalog.diagnostics)
        return 0
    lines = ["Skills:"]
    for skill in catalog.skills:
        source_id = "plugin" if skill.source == "plugin" else f"{skill.origin}.{skill.source}"
        source = f"[{source_id}]"
        state = "[enabled]" if skill.enabled else "[disabled]"
        managed = "[managed]" if skill.managed else "[external]"
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
        loaded = ConfigurationFacade(home).skill(name)
    except ConfigError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1
    except (OuroError, OSError, ValueError) as exc:
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

    skill = loaded.skill
    lines = [
        f"Skill: {skill.name}",
        "Source: " + ("plugin" if skill.source == "plugin" else f"{skill.origin}.{skill.source}"),
        f"Enabled: {'yes' if skill.enabled else 'no'}",
        f"Location: {skill.location}",
    ]
    if skill.description:
        lines.append(f"Description: {skill.description}")
    if skill.compatibility:
        lines.append(f"Compatibility: {skill.compatibility}")
    if skill.allowed_tools:
        lines.append(f"Allowed tools: {skill.allowed_tools}")
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
        roots = ConfigurationFacade(home).skill_roots()
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
        exists = "exists" if root.exists else "missing"
        enabled = "enabled" if root.enabled else "disabled"
        lines.append(f"- {root.source_id}: {root.path} ({enabled}, {exists})")
    print("\n".join(lines))
    return 0


def show_skills_prompt(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        prompt = ConfigurationFacade(home).skills_prompt()
    except OuroError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "skill_count": prompt.skill_count,
                    "prompt": prompt.prompt,
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(prompt.prompt or "No skills prompt: no skills found.")
    return 0


def validate_skills(
    path: str | Path | None = None,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        result = ConfigurationFacade(home).validate_skills(path)
    except OuroError as exc:
        _print_skill_error(str(exc), json_mode=json_mode)
        return 1

    payload = {
        "status": "ok" if result.valid else "failed",
        "valid": result.valid,
        "skills": [_skill_metadata_to_dict(skill) for skill in result.skills],
        "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Validated {len(result.skills)} skill(s): {'valid' if result.valid else 'failed'}")
        _print_diagnostics(result.diagnostics)
    return 0 if result.valid else 1


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
        lambda: ConfigurationFacade(home).install_skills(
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
        lambda: ConfigurationFacade(home).update_skill(name),
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
        lambda: ConfigurationFacade(home).remove_skill(name, force=force),
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
        lambda: ConfigurationFacade(home).set_skill_enabled(name, enabled=enabled),
        json_mode=json_mode,
    )


def _skill_metadata_to_dict(skill: SkillView) -> dict[str, object]:
    return skill.model_dump(mode="json", exclude={"managed", "install"})


def _loaded_skill_to_dict(loaded: LoadedSkillView) -> dict[str, object]:
    return {
        **_skill_metadata_to_dict(loaded.skill),
        "content": loaded.content,
    }


def _skill_root_to_dict(root: SkillRootView) -> dict[str, str | bool]:
    return {
        "path": root.path,
        "source": root.source,
        "source_id": root.source_id,
        "origin": root.origin,
        "enabled": root.enabled,
        "exists": root.exists,
    }


def _print_skill_error(message: str, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False))
    else:
        print(f"Error: {message}", file=sys.stderr)


def _print_diagnostics(diagnostics: Sequence[SkillDiagnosticView]) -> None:
    for item in diagnostics:
        print(
            f"{item.level.title()}: [{item.code}] {item.message} ({item.path})",
            file=sys.stderr,
        )


def _run_management(
    operation: Callable[[], SkillManagementView],
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


def _management_result_to_dict(result: SkillManagementView) -> dict[str, object]:
    return {
        "status": "ok",
        "action": result.action,
        "skills": [skill.model_dump(mode="json") for skill in result.skills],
        "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
    }
