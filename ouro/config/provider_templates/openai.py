"""OpenAI API and Codex subscription presets."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig, endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_responses_effort_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "gpt-6.2-sol"

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

_OPENAI_ALWAYS_ON_REASONING = openai_responses_effort_reasoning(
    supported_summaries=("auto",),
    default=ReasoningRequest(summary="auto"),
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
)


_MODEL_DEFAULTS = {"context_window": 1_050_000, "supports_image_input": True}

_MODELS: dict[str, dict[str, object]] = {
    "gpt-6.2-astra": {"display_name": "GPT-6.2 Astra", "reasoning": _OPENAI_ALWAYS_ON_REASONING},
    _DEFAULT_MODEL: {"display_name": "GPT-6.2 Sol", "reasoning": _OPENAI_ALWAYS_ON_REASONING},
    "gpt-6-astra": {"display_name": "GPT-6 Astra", "reasoning": _OPENAI_ALWAYS_ON_REASONING},
    "gpt-6.1-sol": {"display_name": "GPT-6.1 Sol", "reasoning": _OPENAI_ALWAYS_ON_REASONING},
    "gpt-6-sol": {"display_name": "GPT-6 Sol", "reasoning": _OPENAI_56_REASONING},
    "gpt-6-luna": {"display_name": "GPT-6 Luna", "reasoning": _OPENAI_56_REASONING},
    "gpt-5.6-sol": {"display_name": "GPT-5.6 Sol", "reasoning": _OPENAI_56_REASONING},
    "gpt-5.6-terra": {"display_name": "GPT-5.6 Terra", "reasoning": _OPENAI_56_REASONING},
    "gpt-5.6-luna": {"display_name": "GPT-5.6 Luna", "reasoning": _OPENAI_56_REASONING},
    "gpt-5.5": {
        "display_name": "GPT-5.5",
        "reasoning": _OPENAI_55_REASONING,
        "api_only": True,
    },
}


def _endpoints(*, codex: bool) -> tuple[ProviderEndpointConfig, ...]:
    endpoints = []
    for model_id, settings in _MODELS.items():
        options = {**_MODEL_DEFAULTS, **settings, "model_id": model_id}
        if options.pop("api_only", False) and codex:
            continue
        if codex:
            options.update(reasoning=_OPENAI_ALWAYS_ON_REASONING, supports_structured_output=False)
        else:
            options["supports_inline_system_messages"] = True
        endpoints.append(endpoint_config_from_mapping(model_id, options))
    return tuple(endpoints)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="openai-codex",
        display_name="OpenAI Codex (ChatGPT subscription)",
        description=(
            "Browser subscription login with the documented Codex model catalog. "
            "Availability depends on the account and rollout. "
            "No request output limit is configured; the local context window is 1050000. "
            "Image input is enabled; "
            "structured output is not declared supported by this integration."
        ),
        provider_type="openai-codex",
        protocol="openai-responses",
        auth_driver="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_path="/responses",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=_endpoints(codex=True),
        documentation_url="https://learn.chatgpt.com/docs/models",
        verified_on=_VERIFIED_ON,
        revision=15,
    ),
    ProviderTemplate(
        template_id="openai",
        display_name="OpenAI",
        description=(
            "OpenAI Responses API with GPT-6.2 Astra and Sol, GPT-6.1 Sol, and earlier models. "
            "Local context window 1050000 with no configured output limit. "
            "GPT-6 tool calling requires Responses. "
            "Image input is supported by the OURO Responses adapter."
        ),
        provider_type="openai",
        protocol="openai-responses",
        credential_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        api_path="/responses",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=_endpoints(codex=False),
        documentation_url="https://developers.openai.com/api/docs/models",
        verified_on=_VERIFIED_ON,
        revision=14,
    ),
)
