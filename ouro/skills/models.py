from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ouro.core.base import OuroModel

SkillSource = Literal["user", "workspace", "plugin"]
DiagnosticLevel = Literal["warning", "error"]


class SkillDiagnostic(OuroModel):
    level: DiagnosticLevel = "warning"
    code: str
    message: str
    path: str


class SkillMetadata(OuroModel):
    """Catalog metadata parsed from a standard Agent Skills ``SKILL.md`` file."""

    name: str
    description: str
    location: str
    source: SkillSource = "user"
    enabled: bool = True
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: str | None = None
    user_invocable: bool = True
    disable_model_invocation: bool = False
    argument_hint: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)

    @property
    def skill_id(self) -> str:
        """Backward-compatible lookup name; the standard calls this field ``name``."""

        return self.name

    @property
    def path(self) -> str:
        """Backward-compatible skill directory derived from ``location``."""

        return str(Path(self.location).parent)


class LoadedSkill(OuroModel):
    metadata: SkillMetadata
    content: str
    resources: list[str] = Field(default_factory=list)


class SkillCatalog(OuroModel):
    skills: list[SkillMetadata] = Field(default_factory=list)
    diagnostics: list[SkillDiagnostic] = Field(default_factory=list)

    def by_name(self) -> dict[str, SkillMetadata]:
        return {skill.name: skill for skill in self.skills}
