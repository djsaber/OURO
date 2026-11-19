"""Deepseek provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "deepseek-v4-pro"

_DEEPSEEK_REASONING = mapped_reasoning(
    name="deepseek-thinking-effort",
    activation="toggle",
    supported_efforts=("low", "high", "max"),
    default=ReasoningRequest(mode="enabled", effort="high"),
    effort={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {"thinking": {"type": "enabled"}},
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "max": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    },
)


_MODEL_DEFAULTS = {
    "context_window": 1_000_000,
    "reasoning": _DEEPSEEK_REASONING,
    "reasoning_text_field": "reasoning_content",
}

_MODELS: dict[str, dict[str, object]] = {
    _DEFAULT_MODEL: {"display_name": "DeepSeek V4 Pro"},
    "deepseek-flash": {
        "display_name": "DeepSeek V4.1 Flash 1017",
        "supports_image_input": True,
    },
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="deepseek",
        display_name="DeepSeek",
        description=(
            "DeepSeek's Chat Completions API with V4 Pro and V4.1 Flash 1017 (deepseek-flash). "
            "No request output limit is configured."
        ),
        provider_type="deepseek",
        protocol="openai-chat-completions",
        credential_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        api_path="/chat/completions",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(
                model_id, {**_MODEL_DEFAULTS, **settings, "model_id": model_id}
            )
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://api-docs.deepseek.com/quick_start/pricing/",
        verified_on=_VERIFIED_ON,
        revision=13,
    ),
)
