"""Doubao provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning
from ouro.core.reasoning import ReasoningEffort, ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "doubao-seed-2-1-pro-260915"
_DOUBAO_EFFORTS: tuple[ReasoningEffort, ...] = ("low", "medium", "high")

_DOUBAO_REASONING = mapped_reasoning(
    name="doubao-thinking-effort",
    activation="toggle",
    supported_efforts=_DOUBAO_EFFORTS,
    default=ReasoningRequest(mode="enabled", effort="high"),
    effort={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        **{
            effort: {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
            for effort in _DOUBAO_EFFORTS
        },
    },
)


_MODEL_DEFAULTS = {
    "context_window": 1_048_576,
    "supports_image_input": True,
    "supports_video_input": True,
    "reasoning": _DOUBAO_REASONING,
    "reasoning_text_field": "reasoning_content",
}

_MODELS: dict[str, dict[str, object]] = {
    _DEFAULT_MODEL: {"display_name": "Doubao Seed 2.1 Pro"},
    "doubao-seed-evolving": {"display_name": "Doubao Seed Evolving"},
    "doubao-seed-2-1-lite-260915": {
        "display_name": "Doubao Seed 2.1 Lite",
        "supports_audio_input": True,
    },
    "doubao-seed-2-1-turbo-260628": {
        "display_name": "Doubao Seed 2.1 Turbo",
        "context_window": 262_144,
    },
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="doubao",
        display_name="ByteDance Doubao (Volcengine Ark)",
        description=(
            "Doubao Seed models on Volcengine Ark's China Chat Completions endpoint. "
            "Enable the model in Ark before using its public model ID."
        ),
        provider_type="doubao",
        protocol="openai-chat-completions",
        credential_env="ARK_API_KEY",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_path="/chat/completions",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(
                model_id, {**_MODEL_DEFAULTS, **settings, "model_id": model_id}
            )
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://console.volcengine.com/ark/region:cn-beijing/docs/model-list",
        verified_on=_VERIFIED_ON,
        revision=9,
    ),
)
