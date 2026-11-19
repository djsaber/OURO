"""Reviewed OpenCode Go model catalog.

Routes: https://opencode.ai/docs/console/go/
Limits and capabilities: https://models.dev/api.json, opencode-go provider.
Only image attachments are enabled: audio/video wire compatibility through Go
is not established by the model modality list alone.
"""

from typing import cast

from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_headers import ContextHeaderValue
from ouro.config.provider_models import ProviderType
from ouro.config.provider_templates.schema import ProviderTemplate

_VERIFIED_ON = "2026-11-19"
_DEFAULT_CHAT_MODEL = "kimi-k3"
_DEFAULT_RESPONSES_MODEL = "gpt-6-luna"
_DEFAULT_MESSAGES_MODEL = "minimax-m3"

_CHAT_MODEL_DEFAULTS: dict[str, object] = {}
_RESPONSES_MODEL_DEFAULTS: dict[str, object] = {"supports_image_input": True}
_MESSAGES_MODEL_DEFAULTS: dict[str, object] = {
    "context_window": 1_000_000,
    "max_output_tokens": 32_768,
    "supports_image_input": True,
    "supports_structured_output": False,
}

_MODELS: dict[str, dict[str, object]] = {
    "glm-5.3-flash": {
        "display_name": "GLM-5.3-Flash",
        "context_window": 1_000_000,
        "supports_image_input": True,
        "reasoning_text_field": "reasoning_content",
    },
    "glm-5.3": {
        "display_name": "GLM-5.3",
        "context_window": 1_000_000,
        "reasoning_text_field": "reasoning_content",
    },
    "glm-5.2": {
        "display_name": "GLM-5.2",
        "context_window": 1_000_000,
        "reasoning_text_field": "reasoning_content",
    },
    "kimi-k3.1-pro": {
        "display_name": "Kimi K3.1 Pro",
        "context_window": 1_048_576,
        "supports_image_input": True,
        "reasoning_text_field": "reasoning_content",
    },
    _DEFAULT_CHAT_MODEL: {
        "display_name": "Kimi K3",
        "context_window": 1_048_576,
        "supports_image_input": True,
        "reasoning_text_field": "reasoning_content",
    },
    "kimi-k2.7-code": {
        "display_name": "Kimi K2.7 Code",
        "context_window": 262_144,
        "supports_image_input": True,
        "reasoning_text_field": "reasoning_content",
    },
    "longcat-2.0": {
        "display_name": "LongCat-2.0",
        "context_window": 1_000_000,
        "supports_structured_output": False,
        "reasoning_text_field": "reasoning_content",
    },
    "deepseek-v4.1-flash": {
        "display_name": "DeepSeek V4.1 Flash 1017",
        "context_window": 1_000_000,
        "supports_image_input": True,
        "reasoning_text_field": "reasoning_content",
    },
    "deepseek-v4-pro": {
        "display_name": "DeepSeek V4 Pro",
        "context_window": 1_000_000,
        "reasoning_text_field": "reasoning_content",
    },
    "mimo-v2.6-flash": {
        "display_name": "MiMo-V2.6-Flash",
        "context_window": 1_048_576,
        "supports_image_input": True,
        "supports_structured_output": False,
        "reasoning_text_field": "reasoning_content",
    },
    "mimo-v2.6-pro": {
        "display_name": "MiMo-V2.6-Pro",
        "context_window": 1_048_576,
        "supports_image_input": True,
        "supports_structured_output": False,
        "reasoning_text_field": "reasoning_content",
    },
    "mimo-v2.5": {
        "display_name": "MiMo V2.5",
        "context_window": 1_000_000,
        "supports_image_input": True,
        "supports_structured_output": False,
        "reasoning_text_field": "reasoning_content",
    },
    "mimo-v2.5-pro": {
        "display_name": "MiMo V2.5 Pro",
        "context_window": 1_048_576,
        "supports_structured_output": False,
        "reasoning_text_field": "reasoning_content",
    },
    "minimax-m3.1-flash": {
        "display_name": "MiniMax M3.1 Flash",
        "context_window": 1_000_000,
        "supports_image_input": True,
        "supports_structured_output": False,
        "reasoning_text_field": "reasoning_content",
    },
    "hy4": {
        "display_name": "Hy4",
        "context_window": 1_048_576,
        "supports_image_input": True,
        "supports_structured_output": False,
        "metadata": {"provider_input_limit": 983_010},
    },
    "hy4-flash-free": {
        "display_name": "Hy4 Flash Free",
        "context_window": 1_048_576,
        "supports_image_input": True,
        "supports_structured_output": False,
        "metadata": {"provider_input_limit": 983_040},
    },
    "hy4-preview": {
        "display_name": "Hy4 preview",
        "context_window": 1_024_000,
        "supports_structured_output": False,
    },
    "hy3": {
        "display_name": "Hy3",
        "context_window": 256_000,
        "supports_structured_output": False,
        "metadata": {"provider_input_limit": 192_000},
    },
    "step-5-preview": {
        "display_name": "Step 5 Preview",
        "context_window": 1_000_000,
        "supports_image_input": True,
        "metadata": {"provider_input_limit": 1_000_000},
        "reasoning_text_field": "reasoning_content",
    },
    "beam-free": {
        "display_name": "Beam Free",
        "context_window": 1_048_576,
        "reasoning_text_field": "reasoning_content",
    },
    "iceman-contributor-free": {
        "display_name": "Iceman Contributor Free",
        "context_window": 262_144,
        "supports_structured_output": False,
    },
    "longcat-2.5-free": {
        "display_name": "LongCat 2.5 Free",
        "context_window": 1_000_000,
        "supports_image_input": True,
        "supports_structured_output": False,
        "reasoning_text_field": "reasoning_content",
    },
    "grok-4.7": {
        "protocol": "openai-responses",
        "display_name": "Grok 4.7",
        "context_window": 500_000,
    },
    _DEFAULT_RESPONSES_MODEL: {
        "protocol": "openai-responses",
        "display_name": "GPT-6 Luna",
        "context_window": 1_050_000,
        "metadata": {"provider_input_limit": 922_000},
    },
    "gpt-5.6-luna": {
        "protocol": "openai-responses",
        "display_name": "GPT-5.6 Luna",
        "context_window": 1_050_000,
        "metadata": {"provider_input_limit": 922_000},
    },
    "muse-spark-1.3-contributor": {
        "protocol": "openai-responses",
        "display_name": "Muse Spark 1.3 Contributor",
        "context_window": 1_048_576,
    },
    "muse-spark-1.2-contributor": {
        "protocol": "openai-responses",
        "display_name": "Muse Spark 1.2 Contributor",
        "context_window": 1_048_576,
    },
    "claude-haiku-5-5": {"protocol": "anthropic-messages", "display_name": "Claude Haiku 5.5"},
    _DEFAULT_MESSAGES_MODEL: {"protocol": "anthropic-messages", "display_name": "MiniMax-M3"},
    "qwen4-max": {"protocol": "anthropic-messages", "display_name": "Qwen4 Max"},
    "qwen3.8-max": {"protocol": "anthropic-messages", "display_name": "Qwen3.8 Max"},
    "qwen3.8-flash": {"protocol": "anthropic-messages", "display_name": "Qwen3.8 Flash"},
    "qwen3.7-plus": {"protocol": "anthropic-messages", "display_name": "Qwen3.7 Plus"},
}


TEMPLATES: tuple[ProviderTemplate, ...] = tuple(
    ProviderTemplate(
        template_id=template_id,
        display_name=f"OpenCode Go ({label})",
        description=f"OpenCode Go {label} models with automatic session headers.",
        provider_type=cast(ProviderType, template_id),
        protocol=protocol,
        credential_env="OPENCODE_API_KEY",
        base_url=f"https://opencode.ai/inference/go/{gateway}/v1",
        api_path=api_path,
        headers={"x-opencode-session": ContextHeaderValue(key="session.id")},
        default_endpoint=default_model,
        endpoints=tuple(
            endpoint_config_from_mapping(
                model_id,
                {
                    **model_defaults,
                    **{field: value for field, value in settings.items() if field != "protocol"},
                    "model_id": model_id,
                },
            )
            for model_id, settings in _MODELS.items()
            if settings.get("protocol", "openai-chat-completions") == protocol
        ),
        documentation_url="https://opencode.ai/docs/console/go/",
        verified_on=_VERIFIED_ON,
        revision=6,
    )
    for template_id, label, protocol, gateway, api_path, default_model, model_defaults in (
        (
            "opencode-go",
            "Chat Completions",
            "openai-chat-completions",
            "openai",
            "/chat/completions",
            _DEFAULT_CHAT_MODEL,
            _CHAT_MODEL_DEFAULTS,
        ),
        (
            "opencode-go-responses",
            "Responses",
            "openai-responses",
            "openai",
            "/responses",
            _DEFAULT_RESPONSES_MODEL,
            _RESPONSES_MODEL_DEFAULTS,
        ),
        (
            "opencode-go-messages",
            "Messages",
            "anthropic-messages",
            "anthropic",
            "/messages",
            _DEFAULT_MESSAGES_MODEL,
            _MESSAGES_MODEL_DEFAULTS,
        ),
    )
)
