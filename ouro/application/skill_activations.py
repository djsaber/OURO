from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field, field_validator

from ouro.core.base import OuroModel
from ouro.core.errors import SkillActivationError
from ouro.core.messages import SkillActivationContent
from ouro.skills import SkillMetadata, load_skill_from_metadata


class SkillInvocationRequest(OuroModel):
    """Host-neutral selector for one explicit user skill invocation."""

    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    arguments: str = ""
    text_offset: int = Field(default=0, ge=0)
    order: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("skill name must not be blank")
        return normalized


class SkillActivationService:
    """Resolve explicit user selections against one already-frozen skill catalog."""

    def list_for_host(self, skills: Sequence[SkillMetadata]) -> tuple[SkillMetadata, ...]:
        return tuple(
            skill.model_copy(deep=True) for skill in sorted(skills, key=lambda item: item.name)
        )

    def resolve_user_activation(
        self,
        skills: Sequence[SkillMetadata],
        request: SkillInvocationRequest,
    ) -> SkillActivationContent:
        matches = [skill for skill in skills if skill.name == request.name]
        if not matches:
            raise SkillActivationError(
                "unknown_skill",
                request.name,
                f"Unknown skill: {request.name}",
            )
        if len(matches) > 1:
            raise RuntimeError(f"frozen skill catalog contains duplicate name: {request.name}")
        metadata = matches[0]
        if not metadata.enabled:
            raise SkillActivationError(
                "disabled_skill",
                request.name,
                f"Skill is disabled: {request.name}",
            )
        if not metadata.user_invocable:
            raise SkillActivationError(
                "not_user_invocable",
                request.name,
                f"Skill does not allow explicit user invocation: {request.name}",
            )
        try:
            loaded = load_skill_from_metadata(metadata)
        except (OSError, ValueError) as exc:
            raise SkillActivationError(
                "unloadable_skill",
                request.name,
                f"Could not load skill {request.name}: {exc}",
            ) from exc
        if not loaded.content.strip():
            raise SkillActivationError(
                "unloadable_skill",
                request.name,
                f"Skill has no instructions: {request.name}",
            )
        return SkillActivationContent(
            name=loaded.metadata.name,
            text_offset=request.text_offset,
            order=request.order,
            source=loaded.metadata.source,
            location=loaded.metadata.location,
            base_directory=str(Path(loaded.metadata.location).parent),
            instructions=loaded.content,
            content_digest=_skill_content_digest(loaded.metadata, loaded.content),
        )


def _skill_content_digest(metadata: SkillMetadata, instructions: str) -> str:
    payload = {
        "metadata": metadata.model_dump(mode="json"),
        "instructions": instructions,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = ["SkillActivationService", "SkillInvocationRequest"]
