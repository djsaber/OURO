from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ouro.config.skills import SkillConfig, SkillConfigManager
from ouro.skills.loader import SkillLoader
from ouro.skills.models import SkillCatalog, SkillDiagnostic, SkillMetadata, SkillSource


@dataclass(frozen=True)
class SkillRoot:
    path: Path
    source: SkillSource
    origin: str = "configured"


def discover_skill_catalog(
    *,
    home: str | Path | None = None,
    cwd: str | Path | None = None,
    user_home: str | Path | None = None,
    include_disabled: bool = False,
) -> SkillCatalog:
    ouro_home = _ouro_home(home)
    config = SkillConfigManager(ouro_home).snapshot().config
    if not config.enabled:
        return SkillCatalog()
    roots = default_skill_roots(
        home=ouro_home,
        cwd=cwd,
        user_home=user_home,
        config=config,
    )
    return skill_catalog_from_roots(
        roots,
        disabled=config.disabled,
        include_disabled=include_disabled,
        max_depth=config.max_depth,
        max_directories=config.max_directories,
    )


def discover_available_skills(
    *,
    home: str | Path | None = None,
    cwd: str | Path | None = None,
    user_home: str | Path | None = None,
) -> list[SkillMetadata]:
    return discover_skill_catalog(home=home, cwd=cwd, user_home=user_home).skills


def available_skills_from_roots(roots: list[SkillRoot]) -> list[SkillMetadata]:
    return skill_catalog_from_roots(roots).skills


def skill_catalog_from_roots(
    roots: list[SkillRoot],
    *,
    disabled: set[str] | frozenset[str] = frozenset(),
    include_disabled: bool = False,
    max_depth: int = 6,
    max_directories: int = 2000,
) -> SkillCatalog:
    merged: dict[str, SkillMetadata] = {}
    diagnostics: list[SkillDiagnostic] = []
    for root in roots:
        loader = SkillLoader(
            root.path,
            default_source=root.source,
            max_depth=max_depth,
            max_directories=max_directories,
        )
        catalog = loader.load_catalog()
        diagnostics.extend(catalog.diagnostics)
        for metadata in catalog.skills:
            if metadata.name in disabled:
                if not include_disabled:
                    continue
                metadata = metadata.model_copy(update={"enabled": False})
            existing = merged.get(metadata.name)
            if existing is not None:
                diagnostics.append(
                    SkillDiagnostic(
                        code="shadowed_skill",
                        message=(
                            f"{metadata.location} overrides {existing.location} for skill "
                            f"{metadata.name!r}"
                        ),
                        path=metadata.location,
                    )
                )
            merged[metadata.name] = metadata
    return SkillCatalog(
        skills=sorted(merged.values(), key=lambda item: item.name),
        diagnostics=diagnostics,
    )


def default_skill_roots(
    *,
    home: str | Path | None = None,
    cwd: str | Path | None = None,
    user_home: str | Path | None = None,
    config: SkillConfig | None = None,
) -> list[SkillRoot]:
    ouro_home = _ouro_home(home)
    workspace = Path(cwd) if cwd is not None else Path.cwd()
    profile_home = _profile_home(home=home, ouro_home=ouro_home, user_home=user_home)
    config = config or SkillConfigManager(ouro_home).snapshot().config

    user_roots: list[SkillRoot] = []
    workspace_roots: list[SkillRoot] = []
    if config.scan_agents:
        user_roots.append(SkillRoot(profile_home / ".agents" / "skills", "user", "agents"))
    if config.scan_claude:
        user_roots.append(SkillRoot(profile_home / ".claude" / "skills", "user", "claude"))
    if config.scan_copilot:
        user_roots.append(SkillRoot(profile_home / ".copilot" / "skills", "user", "copilot"))
    if config.scan_codex:
        user_roots.append(SkillRoot(profile_home / ".codex" / "skills", "user", "codex"))
    user_roots.append(SkillRoot(ouro_home / "skills", "user", "ouro"))

    if config.scan_agents:
        workspace_roots.append(SkillRoot(workspace / ".agents" / "skills", "workspace", "agents"))
    if config.scan_claude:
        workspace_roots.append(SkillRoot(workspace / ".claude" / "skills", "workspace", "claude"))
    if config.scan_github:
        workspace_roots.append(SkillRoot(workspace / ".github" / "skills", "workspace", "github"))
    if config.scan_codex:
        workspace_roots.append(SkillRoot(workspace / ".codex" / "skills", "workspace", "codex"))
    workspace_roots.append(SkillRoot(workspace / ".ouro" / "skills", "workspace", "ouro"))

    for item in config.paths:
        base = workspace if item.source == "workspace" else ouro_home
        path = Path(item.path).expanduser()
        if not path.is_absolute():
            path = base / path
        root = SkillRoot(path, item.source, "configured")
        if item.source == "workspace":
            workspace_roots.append(root)
        else:
            user_roots.append(root)
    return _dedupe_roots([*user_roots, *workspace_roots])


def _ouro_home(home: str | Path | None) -> Path:
    root_value = home if home is not None else os.environ.get("OURO_HOME")
    return (
        (Path(root_value) if root_value is not None else Path.home() / ".ouro")
        .expanduser()
        .resolve()
    )


def _profile_home(
    *,
    home: str | Path | None,
    ouro_home: Path,
    user_home: str | Path | None,
) -> Path:
    if user_home is not None:
        return Path(user_home).expanduser().resolve()
    default_ouro_home = (Path.home() / ".ouro").resolve()
    if home is None or ouro_home == default_ouro_home:
        return Path.home().resolve()
    return ouro_home


def _dedupe_roots(candidates: list[SkillRoot]) -> list[SkillRoot]:
    roots: list[SkillRoot] = []
    positions: dict[Path, int] = {}
    for candidate in candidates:
        resolved = candidate.path.expanduser().resolve()
        normalized = SkillRoot(resolved, candidate.source, candidate.origin)
        previous = positions.get(resolved)
        if previous is not None:
            roots.pop(previous)
            positions = {root.path: index for index, root in enumerate(roots)}
        positions[resolved] = len(roots)
        roots.append(normalized)
    return roots
