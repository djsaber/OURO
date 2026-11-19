"""Kimi provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "kimi-k3.1-pro"

_KIMI_K3_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "high", "max"),
    default=ReasoningRequest(mode="enabled", effort="max"),
)


_MODEL_DEFAULTS = {
    "context_window": 1_048_576,
    "supports_image_input": True,
    "supports_video_input": True,
    "reasoning": _KIMI_K3_REASONING,
    "reasoning_text_field": "reasoning_content",
}

_MODELS: dict[str, dict[str, object]] = {
    _DEFAULT_MODEL: {"display_name": "Kimi K3.1 Pro"},
    "kimi-k3.1-pro-highspeed": {"display_name": "Kimi K3.1 Pro Highspeed"},
    "kimi-k3": {"display_name": "Kimi K3"},
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="kimi",
        display_name="Moonshot AI Kimi",
        description="Kimi's Chat Completions API with K3.1 Pro and K3 multimodal models.",
        provider_type="kimi",
        protocol="openai-chat-completions",
        credential_env="MOONSHOT_API_KEY",
        base_url="https://api.moonshot.ai/v1",
        api_path="/chat/completions",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(
                model_id, {**_MODEL_DEFAULTS, **settings, "model_id": model_id}
            )
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://platform.kimi.ai/docs/models",
        verified_on=_VERIFIED_ON,
        revision=12,
    ),
)
