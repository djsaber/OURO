"""Grok provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "grok-4.7"

_GROK_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh"),
    default=ReasoningRequest(mode="enabled", effort="high"),
)


_MODELS: dict[str, dict[str, object]] = {
    _DEFAULT_MODEL: {
        "display_name": "Grok 4.7",
        "context_window": 500_000,
        "supports_image_input": True,
        "reasoning": _GROK_REASONING,
    },
}


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
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(model_id, {**settings, "model_id": model_id})
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://docs.x.ai/developers/grok-4-7",
        verified_on=_VERIFIED_ON,
        revision=9,
    ),
)
