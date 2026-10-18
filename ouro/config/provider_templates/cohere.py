"""Cohere provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_COHERE_REASONING = openai_chat_completions_effort_reasoning(
    supported_efforts=("high",),
    default=ReasoningRequest(mode="enabled", effort="high"),
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        revision=4,
        template_id="cohere",
        display_name="Cohere",
        description="Cohere's OpenAI-compatible API with the multilingual Command A+ model.",
        provider_type="cohere",
        protocol="openai-chat-completions",
        credential_env="COHERE_API_KEY",
        base_url="https://api.cohere.ai/compatibility/v1",
        api_path="/chat/completions",
        default_endpoint="command-a-plus-05-2026",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="command-a-plus-05-2026",
                display_name="Command A+",
                model_id="command-a-plus-05-2026",
                context_window=128_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_COHERE_REASONING,
            ),
        ),
        documentation_url="https://docs.cohere.com/docs/compatibility-api",
        verified_on=_VERIFIED_ON,
    ),
)
