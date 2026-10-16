"""Gemini provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_GEMINI_FLASH_REASONING = openai_chat_completions_effort_reasoning(
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
        default_endpoint="gemini-3.8-flash",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="gemini-3.8-flash",
                display_name="Gemini 3.8 Flash",
                model_id="gemini-3.8-flash",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_audio_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GEMINI_FLASH_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="gemini-3.1-pro-preview",
                display_name="Gemini 3.1 Pro Preview",
                model_id="gemini-3.1-pro-preview",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_audio_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GEMINI_PRO_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="gemini-3.5-flash-lite",
                display_name="Gemini 3.5 Flash Lite",
                model_id="gemini-3.5-flash-lite",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_audio_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GEMINI_FLASH_LITE_REASONING,
            ),
        ),
        documentation_url="https://ai.google.dev/gemini-api/docs/models",
        verified_on=_VERIFIED_ON,
        revision=7,
    ),
)
