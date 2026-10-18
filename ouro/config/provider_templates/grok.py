"""Grok provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_GROK_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh"),
    default=ReasoningRequest(mode="enabled", effort="high"),
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="grok",
        display_name="xAI Grok",
        description="xAI's Chat Completions API with the latest Grok frontier model.",
        provider_type="grok",
        protocol="openai-chat-completions",
        credential_env="XAI_API_KEY",
        base_url="https://api.x.ai/v1",
        api_path="/chat/completions",
        default_endpoint="grok-4.7",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="grok-4.7",
                display_name="Grok 4.7",
                model_id="grok-4.7",
                context_window=500_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GROK_REASONING,
            ),
        ),
        documentation_url="https://docs.x.ai/developers/grok-4-7",
        verified_on=_VERIFIED_ON,
        revision=8,
    ),
)
