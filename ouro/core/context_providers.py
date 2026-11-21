"""Request-time observations delivered to the session before a model response."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ConfigDict, Field

from ouro.core.base import OuroModel
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import SemanticTag
from ouro.core.request_providers import (
    OrderedProviderRegistry,
    ProviderEvaluation,
    RequestProviderRegistration,
)
from ouro.core.system_prompts import SystemPromptRegistry


class ContextProviderCall(OuroModel):
    harness: HarnessCallSnapshot | None = None
    session_id: str
    run_id: str
    message_count: int = Field(ge=0)
    model_round: int = Field(ge=1)
    workspace: str | None = None


class ContextFragment(OuroModel):
    """An observation delivered at its position in the dialogue."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    role: MessageRole = "harness"
    semantic_tag: SemanticTag | None = None
    revision: str | None = None


class ContextObservation(ContextFragment):
    plugin_id: str
    provider_id: str
    generation_id: str | None = None
    fragment_index: int = Field(ge=0)


class ContextProviderResult(OuroModel):
    fragments: list[ContextFragment] = Field(default_factory=list)


class ContextProviderRegistration(
    RequestProviderRegistration[ContextProviderCall, ContextProviderResult]
):
    """Produce ordered observations for one model turn."""


class ContextProviderRegistry(OrderedProviderRegistry[ContextProviderRegistration]):
    async def evaluate(self, call: ContextProviderCall) -> ProviderEvaluation[ContextObservation]:
        return await self._evaluate(call, ContextProviderResult, _attribute_observations)


@dataclass(frozen=True)
class RequestProviders:
    """The two sources of request content in one decision's catalog."""

    system_prompts: SystemPromptRegistry
    observations: ContextProviderRegistry


def _attribute_observations(
    provider: ContextProviderRegistration, result: ContextProviderResult
) -> list[ContextObservation]:
    return [
        ContextObservation(
            text=fragment.text,
            role=fragment.role,
            semantic_tag=fragment.semantic_tag,
            revision=fragment.revision,
            plugin_id=provider.plugin_id,
            provider_id=provider.provider_id,
            generation_id=provider.generation_id,
            fragment_index=index,
        )
        for index, fragment in enumerate(result.fragments)
    ]
