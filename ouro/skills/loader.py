from __future__ import annotations

from pathlib import Path

from ouro.skills.models import (
    LoadedSkill,
    SkillCatalog,
    SkillDiagnostic,
    SkillMetadata,
    SkillOrigin,
    SkillSource,
)
from ouro.skills.parser import SkillDocumentParser

_SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}


class SkillLoader:
    """Discover and activate standard Agent Skills below one configured root."""

    def __init__(
        self,
        root: str | Path,
        *,
        default_source: SkillSource = "user",
        default_origin: SkillOrigin = "configured",
        max_depth: int = 6,
        max_directories: int = 2000,
        parser: SkillDocumentParser | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.default_source = default_source
        self.default_origin = default_origin
        self.max_depth = max(0, max_depth)
        self.max_directories = max(1, max_directories)
        self.parser = parser or SkillDocumentParser()
        self._metadata: dict[str, SkillMetadata] = {}
        self._diagnostics: list[SkillDiagnostic] = []

    @property
    def diagnostics(self) -> tuple[SkillDiagnostic, ...]:
        return tuple(self._diagnostics)

    def load_catalog(self) -> SkillCatalog:
        self._metadata = {}
        self._diagnostics = []
        for location in self._skill_paths():
            result = self.parser.parse(
                location,
                source=self.default_source,
                origin=self.default_origin,
            )
            self._diagnostics.extend(result.diagnostics)
            if result.skill is None:
                continue
            metadata = result.skill.metadata
            existing = self._metadata.get(metadata.name)
            if existing is not None:
                self._diagnostics.append(
                    SkillDiagnostic(
                        code="duplicate_name",
                        message=(
                            f"skill {metadata.name!r} at {metadata.location} is shadowed by "
                            f"{existing.location} within the same root"
                        ),
                        path=metadata.location,
                    )
                )
                continue
            self._metadata[metadata.name] = metadata
        return SkillCatalog(
            skills=sorted(self._metadata.values(), key=lambda item: item.name),
            diagnostics=list(self._diagnostics),
        )

    def load_metadata(self) -> list[SkillMetadata]:
        return self.load_catalog().skills

    def load_content(self, skill_name: str) -> LoadedSkill:
        metadata = self._metadata.get(skill_name)
        if metadata is None:
            metadata = self.load_catalog().by_name().get(skill_name)
        if metadata is None:
            raise KeyError(f"unknown skill: {skill_name}")
        return load_skill_from_metadata(metadata, parser=self.parser)

    def _skill_paths(self) -> list[Path]:
        if not self.root.exists() or not self.root.is_dir():
            return []
        direct = self.root / "SKILL.md"
        if direct.is_file():
            return [direct]

        found: list[Path] = []
        stack: list[tuple[Path, int]] = [(self.root, 0)]
        visited: set[Path] = set()
        scanned = 0
        while stack:
            directory, depth = stack.pop()
            try:
                canonical = directory.resolve()
            except OSError:
                canonical = directory
            if canonical in visited:
                continue
            visited.add(canonical)
            scanned += 1
            if scanned > self.max_directories:
                self._diagnostics.append(
                    SkillDiagnostic(
                        code="scan_limit_reached",
                        message=f"stopped after scanning {self.max_directories} directories",
                        path=str(self.root),
                    )
                )
                break
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            except OSError as exc:
                self._diagnostics.append(
                    SkillDiagnostic(
                        code="scan_failed",
                        message=str(exc),
                        path=str(directory),
                    )
                )
                continue
            next_directories: list[Path] = []
            for child in children:
                if not child.is_dir() or child.name in _SKIP_DIRECTORIES:
                    continue
                manifest = child / "SKILL.md"
                if manifest.is_file():
                    found.append(manifest.resolve())
                elif depth < self.max_depth:
                    next_directories.append(child)
            stack.extend((child, depth + 1) for child in reversed(next_directories))
        return sorted(found, key=lambda path: (len(path.parts), str(path).casefold()))


def load_skill_from_metadata(
    metadata: SkillMetadata,
    *,
    parser: SkillDocumentParser | None = None,
) -> LoadedSkill:
    """Load exactly the catalog-selected skill without repeating root discovery."""

    active_parser = parser or SkillDocumentParser()
    result = active_parser.parse(
        metadata.location,
        source=metadata.source,
        origin=metadata.origin,
    )
    if result.skill is None:
        messages = "; ".join(item.message for item in result.diagnostics)
        raise ValueError(f"skill is no longer loadable: {metadata.name}: {messages}")

    parsed_metadata = result.skill.metadata.model_copy(update={"enabled": metadata.enabled})
    if parsed_metadata != metadata:
        raise ValueError(
            f"skill metadata changed after catalog discovery: {metadata.name}; retry the request"
        )
    return LoadedSkill(
        metadata=parsed_metadata,
        content=result.skill.body,
        resources=_resource_paths(Path(metadata.location).parent),
    )


def _resource_paths(skill_dir: Path) -> list[str]:
    resources: list[str] = []
    for path in sorted(skill_dir.rglob("*"), key=lambda item: str(item).casefold()):
        if not path.is_file() or path.name == "SKILL.md":
            continue
        relative = path.relative_to(skill_dir)
        if any(part in _SKIP_DIRECTORIES for part in relative.parts):
            continue
        resources.append(relative.as_posix())
    return resources
