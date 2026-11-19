"""Mistral provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "mistral-large-4-0"

_MISTRAL_REASONING = openai_chat_completions_effort_reasoning(
    supported_efforts=("high",),
    default=ReasoningRequest(mode="disabled", effort="high"),
)
_MISTRAL_LARGE_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high"),
    default=ReasoningRequest(mode="enabled", effort="low"),
)

_MODEL_DEFAULTS = {"supports_image_input": True}

_MODELS: dict[str, dict[str, object]] = {
    "mistral-medium-3-5": {
        "display_name": "Mistral Medium 3.5",
        "context_window": 256_000,
        "reasoning": _MISTRAL_REASONING,
    },
    "mistral-small-2603": {
        "display_name": "Mistral Small 4",
        "context_window": 256_000,
        "reasoning": _MISTRAL_REASONING,
    },
    _DEFAULT_MODEL: {
        "display_name": "Mistral Large 4",
        "context_window": 1_000_000,
        "reasoning": _MISTRAL_LARGE_REASONING,
    },
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="mistral",
        display_name="Mistral AI",
        description="Mistral's multimodal Chat Completions models with adjustable reasoning.",
        provider_type="mistral",
        protocol="openai-chat-completions",
        credential_env="MISTRAL_API_KEY",
        base_url="https://api.mistral.ai/v1",
        api_path="/chat/completions",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(
                model_id, {**_MODEL_DEFAULTS, **settings, "model_id": model_id}
            )
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://docs.mistral.ai/models",
        verified_on=_VERIFIED_ON,
        revision=7,
    ),
)
