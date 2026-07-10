from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ouro.skills.models import SkillDiagnostic, SkillMetadata, SkillSource

_VALID_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER_BOUNDARY = re.compile(r"^---[ \t]*$", re.MULTILINE)
_STANDARD_FIELDS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
    "user-invocable",
    "disable-model-invocation",
    "argument-hint",
}


@dataclass(frozen=True)
class ParsedSkill:
    metadata: SkillMetadata
    body: str
    raw_content: str


@dataclass(frozen=True)
class SkillParseResult:
    skill: ParsedSkill | None
    diagnostics: tuple[SkillDiagnostic, ...]


class SkillDocumentParser:
    """Parse Agent Skills documents strictly first, then apply safe compatibility fallbacks."""

    def parse(self, location: str | Path, *, source: SkillSource = "user") -> SkillParseResult:
        path = Path(location).expanduser().resolve()
        diagnostics: list[SkillDiagnostic] = []
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            return SkillParseResult(
                skill=None,
                diagnostics=(self._error("read_failed", str(exc), path),),
            )

        extracted = self._extract_frontmatter(raw, path)
        if extracted is None:
            return SkillParseResult(
                skill=None,
                diagnostics=(
                    self._error(
                        "missing_frontmatter",
                        "SKILL.md must start with YAML frontmatter",
                        path,
                    ),
                ),
            )
        yaml_text, body = extracted
        frontmatter, yaml_diagnostics = self._parse_yaml(yaml_text, path)
        diagnostics.extend(yaml_diagnostics)
        if frontmatter is None:
            return SkillParseResult(skill=None, diagnostics=tuple(diagnostics))

        parent_name = path.parent.name
        name = self._optional_text(frontmatter.get("name"))
        if not name:
            name = parent_name
            diagnostics.append(
                self._warning(
                    "missing_name",
                    f"frontmatter name is missing; using directory name {parent_name!r}",
                    path,
                )
            )

        description = self._optional_text(frontmatter.get("description"))
        if not description:
            diagnostics.append(
                self._error(
                    "missing_description",
                    "frontmatter description is required for skill discovery",
                    path,
                )
            )
            return SkillParseResult(skill=None, diagnostics=tuple(diagnostics))

        diagnostics.extend(self._name_diagnostics(name, parent_name, path))
        if len(description) > 1024:
            diagnostics.append(
                self._warning(
                    "description_too_long",
                    "description exceeds the Agent Skills 1024 character recommendation",
                    path,
                )
            )

        compatibility = self._optional_text(frontmatter.get("compatibility"))
        if compatibility and len(compatibility) > 500:
            diagnostics.append(
                self._warning(
                    "compatibility_too_long",
                    "compatibility exceeds the Agent Skills 500 character recommendation",
                    path,
                )
            )

        metadata = frontmatter.get("metadata")
        if metadata is None:
            metadata_map: dict[str, Any] = {}
        elif isinstance(metadata, Mapping):
            metadata_map = {str(key): value for key, value in metadata.items()}
        else:
            metadata_map = {}
            diagnostics.append(
                self._warning("invalid_metadata", "metadata should be a mapping", path)
            )

        allowed_tools = self._allowed_tools(frontmatter.get("allowed-tools"), path, diagnostics)
        extensions = {
            str(key): value for key, value in frontmatter.items() if key not in _STANDARD_FIELDS
        }
        skill = ParsedSkill(
            metadata=SkillMetadata(
                name=name,
                description=description,
                location=str(path),
                source=source,
                license=self._optional_text(frontmatter.get("license")),
                compatibility=compatibility,
                metadata=metadata_map,
                allowed_tools=allowed_tools,
                user_invocable=self._optional_bool(
                    frontmatter.get("user-invocable"),
                    default=True,
                ),
                disable_model_invocation=self._optional_bool(
                    frontmatter.get("disable-model-invocation"),
                    default=False,
                ),
                argument_hint=self._optional_text(frontmatter.get("argument-hint")),
                extensions=extensions,
            ),
            body=body.strip(),
            raw_content=raw,
        )
        return SkillParseResult(skill=skill, diagnostics=tuple(diagnostics))

    def _extract_frontmatter(self, raw: str, path: Path) -> tuple[str, str] | None:
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.startswith("---"):
            return None
        boundaries = list(_FRONTMATTER_BOUNDARY.finditer(normalized))
        if len(boundaries) < 2 or boundaries[0].start() != 0:
            return None
        end = boundaries[1]
        return normalized[boundaries[0].end() : end.start()].strip("\n"), normalized[end.end() :]

    def _parse_yaml(
        self,
        text: str,
        path: Path,
    ) -> tuple[dict[str, Any] | None, list[SkillDiagnostic]]:
        diagnostics: list[SkillDiagnostic] = []
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as strict_error:
            repaired = self._repair_top_level_scalar_colons(text)
            if repaired == text:
                return None, [self._error("invalid_yaml", str(strict_error), path)]
            try:
                loaded = yaml.safe_load(repaired)
            except yaml.YAMLError as fallback_error:
                return None, [self._error("invalid_yaml", str(fallback_error), path)]
            diagnostics.append(
                self._warning(
                    "repaired_yaml",
                    "loaded frontmatter after quoting a malformed top-level scalar",
                    path,
                )
            )
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, Mapping):
            return None, [self._error("invalid_frontmatter", "frontmatter must be a mapping", path)]
        return {str(key): value for key, value in loaded.items()}, diagnostics

    def _repair_top_level_scalar_colons(self, text: str) -> str:
        repaired: list[str] = []
        for line in text.splitlines():
            match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]+(.+)$", line)
            if not match:
                repaired.append(line)
                continue
            value = match.group(2).strip()
            if ": " in value and not value.startswith(("'", '"', "|", ">", "[", "{")):
                line = f"{match.group(1)}: {json.dumps(value, ensure_ascii=False)}"
            repaired.append(line)
        return "\n".join(repaired)

    def _name_diagnostics(
        self,
        name: str,
        parent_name: str,
        path: Path,
    ) -> list[SkillDiagnostic]:
        diagnostics: list[SkillDiagnostic] = []
        if name != parent_name:
            diagnostics.append(
                self._warning(
                    "name_directory_mismatch",
                    f"skill name {name!r} does not match directory {parent_name!r}",
                    path,
                )
            )
        if len(name) > 64:
            diagnostics.append(
                self._warning("name_too_long", "skill name exceeds 64 characters", path)
            )
        if not _VALID_NAME.fullmatch(name):
            diagnostics.append(
                self._warning(
                    "nonstandard_name",
                    "skill name is outside the lowercase alphanumeric-and-hyphen convention",
                    path,
                )
            )
        return diagnostics

    def _allowed_tools(
        self,
        value: object,
        path: Path,
        diagnostics: list[SkillDiagnostic],
    ) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            diagnostics.append(
                self._warning(
                    "normalized_allowed_tools",
                    "converted community list-form allowed-tools to the standard string form",
                    path,
                )
            )
            return " ".join(value)
        diagnostics.append(
            self._warning("invalid_allowed_tools", "allowed-tools should be a string", path)
        )
        return None

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, int | float | bool):
            return str(value)
        return None

    @staticmethod
    def _optional_bool(value: object, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
        return default

    @staticmethod
    def _warning(code: str, message: str, path: Path) -> SkillDiagnostic:
        return SkillDiagnostic(code=code, message=message, path=str(path))

    @staticmethod
    def _error(code: str, message: str, path: Path) -> SkillDiagnostic:
        return SkillDiagnostic(level="error", code=code, message=message, path=str(path))
