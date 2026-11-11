"""Kimi provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-11"

_KIMI_K3_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "high", "max"),
    default=ReasoningRequest(mode="enabled", effort="max"),
)


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
        default_endpoint="kimi-k3.1-pro",
        endpoints=tuple(
            ProviderEndpointConfig(
                endpoint_id=model_id,
                display_name=display_name,
                model_id=model_id,
                context_window=1_048_576,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_K3_REASONING,
                reasoning_text_field="reasoning_content",
            )
            for model_id, display_name in (
                ("kimi-k3.1-pro", "Kimi K3.1 Pro"),
                ("kimi-k3.1-pro-highspeed", "Kimi K3.1 Pro Highspeed"),
                ("kimi-k3", "Kimi K3"),
            )
        ),
        documentation_url="https://platform.kimi.ai/docs/models",
        verified_on=_VERIFIED_ON,
        revision=11,
    ),
)
