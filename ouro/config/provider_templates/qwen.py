"""Qwen provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import EndpointReasoningConfig, mapped_reasoning, toggle_reasoning
from ouro.core.reasoning import ReasoningEffort, ReasoningRequest

_VERIFIED_ON = "2026-11-19"
_DEFAULT_MODEL = "qwen4-max"

_THINKING_ENABLED = {"enable_thinking": True, "preserve_thinking": True}
_THINKING_DISABLED = {"enable_thinking": False, "preserve_thinking": False}

_QWEN_REASONING = toggle_reasoning(
    name="qwen-thinking-toggle",
    enabled=_THINKING_ENABLED,
    disabled=_THINKING_DISABLED,
)


def _effort_reasoning(
    name: str, *, supported_efforts: tuple[ReasoningEffort, ...], default_effort: ReasoningEffort
) -> EndpointReasoningConfig:
    return mapped_reasoning(
        name=name,
        activation="toggle",
        supported_efforts=supported_efforts,
        default=ReasoningRequest(mode="enabled", effort=default_effort),
        effort={
            "disabled": _THINKING_DISABLED,
            "enabled": {**_THINKING_ENABLED, "reasoning_effort": default_effort},
            **{
                effort: {**_THINKING_ENABLED, "reasoning_effort": effort}
                for effort in supported_efforts
            },
        },
    )


_QWEN_4_REASONING = _effort_reasoning(
    "qwen-4-thinking-effort", supported_efforts=("low", "high", "xhigh"), default_effort="high"
)
_QWEN_38_REASONING = _effort_reasoning(
    "qwen-3.8-thinking-effort", supported_efforts=("low", "medium", "xhigh"), default_effort="xhigh"
)


_MODEL_DEFAULTS = {
    "context_window": 1_000_000,
    "supports_image_input": True,
    "supports_video_input": True,
    "reasoning_text_field": "reasoning_content",
}

_MODELS: dict[str, dict[str, object]] = {
    _DEFAULT_MODEL: {
        "display_name": "Qwen4 Max",
        "reasoning": _QWEN_4_REASONING,
    },
    "qwen3.8-max": {
        "display_name": "Qwen3.8 Max",
        "reasoning": _QWEN_38_REASONING,
    },
    "qwen3.8-flash": {
        "display_name": "Qwen3.8 Flash",
        "reasoning": _QWEN_38_REASONING,
    },
    "qwen3.7-plus": {
        "display_name": "Qwen3.7 Plus",
        "reasoning": _QWEN_REASONING,
    },
}


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="qwen",
        display_name="Alibaba Cloud Qwen",
        description="Qwen multimodal models on DashScope's China Chat Completions endpoint.",
        provider_type="qwen",
        protocol="openai-chat-completions",
        credential_env="DASHSCOPE_API_KEY",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_path="/chat/completions",
        default_endpoint=_DEFAULT_MODEL,
        endpoints=tuple(
            endpoint_config_from_mapping(
                model_id, {**_MODEL_DEFAULTS, **settings, "model_id": model_id}
            )
            for model_id, settings in _MODELS.items()
        ),
        documentation_url="https://help.aliyun.com/zh/model-studio/text-generation-model",
        verified_on=_VERIFIED_ON,
        revision=14,
    ),
)
