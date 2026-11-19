"""Cohere provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "command-a-plus-05-2026"

_COHERE_REASONING = openai_chat_completions_effort_reasoning(
    supported_efforts=("high",),
    default=ReasoningRequest(mode="enabled", effort="high"),
)


_MODELS: dict[str, dict[str, object]] = {
    _DEFAULT_MODEL: {
        "display_name": "Command A+",
        "context_window": 128_000,
        "supports_image_input": True,
        "reasoning": _COHERE_REASONING,
    },
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        revision=5,
        template_id="cohere",
        display_name="Cohere",
        description="Cohere's OpenAI-compatible API with the multilingual Command A+ model.",
        provider_type="cohere",
        protocol="openai-chat-completions",
        credential_env="COHERE_API_KEY",
        base_url="https://api.cohere.ai/compatibility/v1",
        api_path="/chat/completions",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(model_id, {**settings, "model_id": model_id})
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://docs.cohere.com/docs/compatibility-api",
        verified_on=_VERIFIED_ON,
    ),
)
