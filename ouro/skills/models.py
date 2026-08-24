from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ouro.core.base import OuroModel

SkillSource = Literal["user", "workspace", "plugin"]
SkillOrigin = Literal[
    "ouro",
    "agents",
    "claude",
    "copilot",
    "codex",
    "github",
    "configured",
    "plugin",
]
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
    origin: SkillOrigin = "configured"
    enabled: bool = True
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: str | None = None
    user_invocable: bool = True
    disable_model_invocation: bool = False
    argument_hint: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class LoadedSkill(OuroModel):
    metadata: SkillMetadata
    content: str
    resources: list[str] = Field(default_factory=list)


class SkillCatalog(OuroModel):
    skills: list[SkillMetadata] = Field(default_factory=list)
    diagnostics: list[SkillDiagnostic] = Field(default_factory=list)

    def by_name(self) -> dict[str, SkillMetadata]:
        return {skill.name: skill for skill in self.skills}
