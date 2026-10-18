"""OpenAI API and Codex subscription presets."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_responses_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_OPENAI_55_REASONING = openai_responses_effort_reasoning(
    supported_summaries=("auto",),
    default=ReasoningRequest(summary="auto"),
    supported_efforts=("low", "medium", "high", "xhigh"),
)

_OPENAI_56_REASONING = openai_responses_effort_reasoning(
    supported_summaries=("auto",),
    default=ReasoningRequest(summary="auto"),
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
)

_CODEX_55_REASONING = openai_responses_effort_reasoning(
    supported_summaries=("auto",),
    default=ReasoningRequest(summary="auto"),
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh"),
)

_OPENAI_ALWAYS_ON_REASONING = openai_responses_effort_reasoning(
    supported_summaries=("auto",),
    default=ReasoningRequest(summary="auto"),
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
)

_OPENAI_MODEL_DISPLAY_NAMES = {
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-6.1-sol": "GPT-6.1 Sol",
    "gpt-6-sol": "GPT-6 Sol",
    "gpt-6-luna": "GPT-6 Luna",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.5": "GPT-5.5",
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="openai-codex",
        display_name="OpenAI Codex (ChatGPT subscription)",
        description=(
            "Browser subscription login with the documented Codex model catalog. "
            "Availability depends on the account and rollout. "
            "GPT-5.5 retires from Codex on 2026-10-14. "
            "No request output limit is configured; the local context window is 1050000. "
            "Image input is enabled; "
            "structured output is not declared supported by this integration."
        ),
        provider_type="openai-codex",
        protocol="openai-responses",
        auth_driver="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_path="/responses",
        default_endpoint="gpt-6.1-sol",
        endpoints=tuple(
            ProviderEndpointConfig(
                endpoint_id=model_id,
                display_name=_OPENAI_MODEL_DISPLAY_NAMES[model_id],
                model_id=model_id,
                context_window=1_050_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=reasoning,
            )
            for model_id, reasoning in (
                ("gpt-6-astra", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-6.1-sol", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-6-sol", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-6-luna", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-5.6-sol", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-5.6-terra", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-5.6-luna", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-5.5", _CODEX_55_REASONING),
            )
        ),
        documentation_url="https://learn.chatgpt.com/docs/models",
        verified_on=_VERIFIED_ON,
        revision=13,
    ),
    ProviderTemplate(
        template_id="openai",
        display_name="OpenAI",
        description=(
            "OpenAI Responses API with GPT-6.1 Sol, GPT-6 Astra/Sol/Luna, GPT-5.6 Sol/Terra/Luna "
            "and GPT-5.5. "
            "Local context window 1050000 with no configured output limit. "
            "GPT-6 tool calling requires Responses. "
            "Image input is supported by the OURO Responses adapter."
        ),
        provider_type="openai",
        protocol="openai-responses",
        credential_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        api_path="/responses",
        default_endpoint="gpt-6.1-sol",
        endpoints=tuple(
            ProviderEndpointConfig(
                endpoint_id=model_id,
                display_name=_OPENAI_MODEL_DISPLAY_NAMES[model_id],
                model_id=model_id,
                context_window=1_050_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=reasoning,
            )
            for model_id, reasoning in (
                ("gpt-6-astra", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-6.1-sol", _OPENAI_ALWAYS_ON_REASONING),
                ("gpt-6-sol", _OPENAI_56_REASONING),
                ("gpt-6-luna", _OPENAI_56_REASONING),
                ("gpt-5.6-sol", _OPENAI_56_REASONING),
                ("gpt-5.6-terra", _OPENAI_56_REASONING),
                ("gpt-5.6-luna", _OPENAI_56_REASONING),
                ("gpt-5.5", _OPENAI_55_REASONING),
            )
        ),
        documentation_url="https://developers.openai.com/api/docs/models",
        verified_on=_VERIFIED_ON,
        revision=12,
    ),
)
