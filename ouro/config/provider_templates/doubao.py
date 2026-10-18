"""Doubao provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_DOUBAO_REASONING = mapped_reasoning(
    name="doubao-thinking-effort",
    activation="toggle",
    supported_efforts=("minimal", "low", "medium", "high"),
    default=ReasoningRequest(mode="enabled", effort="high"),
    output="summary",
    continuation="text",
    effort={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        **{
            effort: {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
            for effort in ("minimal", "low", "medium", "high")
        },
    },
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="doubao",
        display_name="ByteDance Doubao (Volcengine Ark)",
        description=(
            "Volcengine Ark's China Chat Completions endpoint with Doubao Seed 2.1. "
            "Enable the model in Ark before using its public model ID."
        ),
        provider_type="doubao",
        protocol="openai-chat-completions",
        credential_env="ARK_API_KEY",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_path="/chat/completions",
        default_endpoint="doubao-seed-2-1-pro-260915",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="doubao-seed-2-1-pro-260915",
                display_name="Doubao Seed 2.1 Pro",
                model_id="doubao-seed-2-1-pro-260915",
                context_window=1_048_576,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DOUBAO_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="doubao-seed-2-1-lite-260915",
                display_name="Doubao Seed 2.1 Lite",
                model_id="doubao-seed-2-1-lite-260915",
                context_window=1_048_576,
                supports_tools=True,
                supports_image_input=True,
                supports_audio_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DOUBAO_REASONING,
            ),
        ),
        documentation_url="https://docs.volcengine.com/docs/ark/model-list?lang=zh",
        verified_on=_VERIFIED_ON,
        revision=5,
    ),
)
