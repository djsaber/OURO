"""Gemini provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "gemini-4-hydrogen"

_GEMINI_4_XHIGH_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh"),
    default=ReasoningRequest(mode="enabled", effort="high"),
)

_GEMINI_MEDIUM_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high"),
    default=ReasoningRequest(mode="enabled", effort="medium"),
)

_GEMINI_PRO_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high"),
    default=ReasoningRequest(mode="enabled", effort="high"),
)

_GEMINI_FLASH_LITE_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("minimal", "low", "medium", "high"),
    default=ReasoningRequest(mode="enabled", effort="minimal"),
)


_MODEL_DEFAULTS = {
    "context_window": 1_048_576,
    "supports_image_input": True,
    "supports_audio_input": True,
}

_MODELS: dict[str, dict[str, object]] = {
    "gemini-4-carbon": {
        "display_name": "Gemini 4 Carbon",
        "reasoning": _GEMINI_MEDIUM_REASONING,
    },
    "gemini-4-neon": {
        "display_name": "Gemini 4 Neon",
        "reasoning": _GEMINI_4_XHIGH_REASONING,
    },
    _DEFAULT_MODEL: {
        "display_name": "Gemini 4 Hydrogen",
        "reasoning": _GEMINI_4_XHIGH_REASONING,
    },
    "gemini-4-argon": {
        "display_name": "Gemini 4 Argon",
        "reasoning": _GEMINI_MEDIUM_REASONING,
    },
    "gemini-3.8-flash": {
        "display_name": "Gemini 3.8 Flash",
        "reasoning": _GEMINI_MEDIUM_REASONING,
    },
    "gemini-3.1-pro-preview": {
        "display_name": "Gemini 3.1 Pro Preview",
        "reasoning": _GEMINI_PRO_REASONING,
    },
    "gemini-3.5-flash-lite": {
        "display_name": "Gemini 3.5 Flash Lite",
        "reasoning": _GEMINI_FLASH_LITE_REASONING,
    },
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="gemini",
        display_name="Google Gemini",
        description="Gemini through Google's official OpenAI Chat Completions-compatible endpoint.",
        provider_type="gemini",
        protocol="openai-chat-completions",
        credential_env="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_path="/chat/completions",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(
                model_id, {**_MODEL_DEFAULTS, **settings, "model_id": model_id}
            )
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://ai.google.dev/gemini-api/docs/models",
        verified_on=_VERIFIED_ON,
        revision=11,
    ),
)
