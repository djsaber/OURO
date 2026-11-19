"""System prompt contributions assembled in provider order for one request."""

from pydantic import ConfigDict, Field

from ouro.core.base import OuroModel
from ouro.core.message_semantics import SemanticTag, render_semantic_envelope
from ouro.core.request_providers import (
    OrderedProviderRegistry,
    ProviderEvaluation,
    RequestProviderRegistration,
)


class SystemPromptFragment(OuroModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    semantic_tag: SemanticTag | None = None

    def render(self) -> str:
        if self.semantic_tag is None:
            return self.text
        return render_semantic_envelope(self.text, self.semantic_tag)


class SystemPromptRegistration(RequestProviderRegistration[str | None, SystemPromptFragment]):
    """Provide one workspace-scoped piece of the system prompt."""


class SystemPromptRegistry(OrderedProviderRegistry[SystemPromptRegistration]):
    async def evaluate(self, workspace: str | None) -> ProviderEvaluation[str]:
        return await self._evaluate(
            workspace, SystemPromptFragment, lambda _, part: (part.render(),)
        )
