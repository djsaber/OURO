from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field, field_validator

from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionRecord
from ouro.core.context_providers import ContextFragment
from ouro.core.errors import SkillActivationError
from ouro.core.messages import (
    MessageRecord,
    ModelMessage,
    SkillActivationContent,
    model_visible_message,
)
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
    """Resolve user selections and capture their current documents as accepted input."""

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
            raise RuntimeError(f"skill catalog contains duplicate name: {request.name}")
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
            loaded = load_skill_from_metadata(metadata, include_resources=False)
        except (OSError, ValueError) as exc:
            raise SkillActivationError(
                "unloadable_skill",
                request.name,
                f"Could not load skill {request.name}: {exc}",
            ) from exc
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

    def resolve_user_activations(
        self,
        skills: Sequence[SkillMetadata],
        requests: Sequence[SkillInvocationRequest],
    ) -> tuple[SkillActivationContent, ...]:
        return tuple(self.resolve_user_activation(skills, request) for request in requests)


def compacted_skill_context(
    messages: Sequence[MessageRecord], active: CompactionRecord | None
) -> list[ContextFragment]:
    """Keep covered skill instructions stable for the lifetime of a compaction projection."""
    if active is None:
        return []
    boundary_run_id = next(
        message.run_id
        for message in messages
        if message.message_id == active.history_boundary_message_id
    )
    if boundary_run_id is None:
        return []
    return [
        ContextFragment(
            role="user",
            text=model_visible_message(
                ModelMessage(role="user", content=list(message.skill_activations))
            ).text_content,
        )
        for message in messages[: len(active.covered_message_ids)]
        if message.run_id == boundary_run_id and message.skill_activations
    ]


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


__all__ = ["SkillActivationService", "SkillInvocationRequest", "compacted_skill_context"]
