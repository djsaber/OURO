"""Mistral provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate

_VERIFIED_ON = "2026-11-11"


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="mistral",
        display_name="Mistral AI",
        description=(
            "Mistral's multimodal Chat Completions models, including Large 4. "
            "The presets use the lowest documented reasoning effort."
        ),
        provider_type="mistral",
        protocol="openai-chat-completions",
        credential_env="MISTRAL_API_KEY",
        base_url="https://api.mistral.ai/v1",
        api_path="/chat/completions",
        default_endpoint="mistral-large-4-0",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="mistral-medium-3-5",
                display_name="Mistral Medium 3.5",
                model_id="mistral-medium-3-5",
                context_window=256_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                request_options={"reasoning_effort": "none"},
            ),
            ProviderEndpointConfig(
                endpoint_id="mistral-small-2603",
                display_name="Mistral Small 4",
                model_id="mistral-small-2603",
                context_window=256_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                request_options={"reasoning_effort": "none"},
            ),
            ProviderEndpointConfig(
                endpoint_id="mistral-large-4-0",
                display_name="Mistral Large 4",
                model_id="mistral-large-4-0",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                request_options={"reasoning_effort": "low"},
            ),
        ),
        documentation_url="https://docs.mistral.ai/models",
        verified_on=_VERIFIED_ON,
        revision=6,
    ),
)
