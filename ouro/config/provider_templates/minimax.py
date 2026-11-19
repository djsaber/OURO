"""MiniMax provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning, toggle_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "MiniMax-M3"

_MINIMAX_REASONING = toggle_reasoning(
    name="minimax-adaptive-thinking",
    enabled={"thinking": {"type": "adaptive"}},
    disabled={"thinking": {"type": "disabled"}},
)

_MINIMAX_FLASH_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default=ReasoningRequest(mode="enabled", effort="max"),
)


_MODEL_DEFAULTS = {
    "context_window": 1_000_000,
    "supports_image_input": True,
    "supports_video_input": True,
    "supports_structured_output": False,
    "reasoning_text_field": "reasoning_content",
    "request_options": {"reasoning_split": True},
}

_MODELS: dict[str, dict[str, object]] = {
    _DEFAULT_MODEL: {
        "display_name": "MiniMax M3",
        "reasoning": _MINIMAX_REASONING,
    },
    "MiniMax-M3.1-Flash": {
        "display_name": "MiniMax M3.1 Flash",
        "reasoning": _MINIMAX_FLASH_REASONING,
    },
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="minimax",
        display_name="MiniMax",
        description=(
            "MiniMax's China Chat Completions endpoint with multimodal M3 and M3.1 Flash."
        ),
        provider_type="minimax",
        protocol="openai-chat-completions",
        credential_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.cn/v1",
        api_path="/chat/completions",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(
                model_id, {**_MODEL_DEFAULTS, **settings, "model_id": model_id}
            )
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://platform.minimax.cn/docs/api-reference/text-openai-api",
        verified_on=_VERIFIED_ON,
        revision=8,
    ),
)
