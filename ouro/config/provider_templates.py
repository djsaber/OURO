from __future__ import annotations

from pydantic import ConfigDict, Field, field_validator, model_validator

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_models import (
    ProviderProtocol,
    ProviderType,
    resolve_provider_connection,
    validate_media_protocol_compatibility,
)
from ouro.config.reasoning import (
    mapped_reasoning,
    openai_chat_completions_effort_reasoning,
    openai_responses_effort_reasoning,
    toggle_reasoning,
)
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.core.reasoning import ReasoningRequest


class ProviderTemplate(OuroModel):
    """A reviewed provider configuration preset with callable model endpoints."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    display_name: str
    description: str
    provider_type: ProviderType
    protocol: ProviderProtocol
    auth_driver: str = "static"
    credential_env: str | None = None
    base_url: str
    api_path: str
    default_endpoint: str
    endpoints: tuple[ProviderEndpointConfig, ...]
    documentation_url: str
    verified_on: str
    revision: int = Field(default=1, ge=1)

    @field_validator("template_id", "display_name", "default_endpoint")
    @classmethod
    def _require_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("credential_env")
    @classmethod
    def _validate_credential_env(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._require_identifier(value)

    @model_validator(mode="after")
    def _validate_endpoint_registry(self) -> ProviderTemplate:
        resolve_provider_connection(self.provider_type, self.protocol, self.auth_driver)
        if self.auth_driver != "static" and self.credential_env is not None:
            raise ValueError("managed templates cannot declare credential_env")
        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if not endpoint_ids:
            raise ValueError("provider template must contain at least one endpoint")
        if len(set(endpoint_ids)) != len(endpoint_ids):
            raise ValueError("provider template endpoint ids must be unique")
        if self.default_endpoint not in endpoint_ids:
            raise ValueError(f"default endpoint not found: {self.default_endpoint}")
        for endpoint in self.endpoints:
            validate_media_protocol_compatibility(
                self.protocol,
                supports_image_input=endpoint.supports_image_input,
                supports_audio_input=endpoint.supports_audio_input,
                supports_video_input=endpoint.supports_video_input,
            )
        return self

    def endpoint(self, endpoint_id: str | None = None) -> ProviderEndpointConfig:
        target = endpoint_id or self.default_endpoint
        endpoint = next(
            (item for item in self.endpoints if item.endpoint_id == target),
            None,
        )
        if endpoint is None:  # Protected by validation for the default lookup.
            raise ConfigError(f"provider template endpoint not found: {self.template_id}/{target}")
        return endpoint


_VERIFIED_ON = "2026-10-10"


_OPENAI_55_REASONING = openai_responses_effort_reasoning(
    supported_efforts=("low", "medium", "high", "xhigh"),
)
_OPENAI_56_REASONING = openai_responses_effort_reasoning(
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
)
_CODEX_55_REASONING = openai_responses_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh"),
)
_OPENAI_ALWAYS_ON_REASONING = openai_responses_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
)
_GEMINI_FLASH_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high"),
    default_effort="medium",
)
_GEMINI_PRO_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high"),
    default_effort="high",
)
_GEMINI_FLASH_LITE_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("minimal", "low", "medium", "high"),
    default_effort="minimal",
)
_DEEPSEEK_REASONING = mapped_reasoning(
    name="deepseek-thinking-effort",
    activation="toggle",
    supported_efforts=("low", "high", "max"),
    default_effort="high",
    default=ReasoningRequest(mode="enabled", effort="high"),
    output="text",
    continuation="text",
    variants={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {"thinking": {"type": "enabled"}},
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "max": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    },
)
_QWEN_REASONING = toggle_reasoning(
    name="qwen-thinking-toggle",
    enabled={"enable_thinking": True, "preserve_thinking": True},
    disabled={"enable_thinking": False, "preserve_thinking": False},
)
_QWEN_FLASH_REASONING = mapped_reasoning(
    name="qwen-flash-thinking-effort",
    activation="toggle",
    supported_efforts=("low", "medium", "xhigh"),
    default_effort="xhigh",
    default=ReasoningRequest(mode="enabled", effort="xhigh"),
    output="text",
    continuation="text",
    variants={
        "disabled": {"enable_thinking": False, "preserve_thinking": False},
        "enabled": {
            "enable_thinking": True,
            "preserve_thinking": True,
            "reasoning_effort": "xhigh",
        },
        **{
            effort: {
                "enable_thinking": True,
                "preserve_thinking": True,
                "reasoning_effort": effort,
            }
            for effort in ("low", "medium", "xhigh")
        },
    },
)
_KIMI_REASONING = toggle_reasoning(
    name="kimi-thinking-toggle",
    enabled={"thinking": {"type": "enabled", "keep": "all"}},
    disabled={"thinking": {"type": "disabled"}},
)
_KIMI_ALWAYS_ON_REASONING = mapped_reasoning(
    name="kimi-always-on-thinking",
    activation="always_on",
    default=ReasoningRequest(mode="enabled"),
    output="text",
    continuation="text",
    variants={"enabled": {}},
)
# Kimi K3 always reasons and uses top-level reasoning_effort.
_KIMI_K3_REASONING = mapped_reasoning(
    name="kimi-k3-reasoning-effort",
    activation="always_on",
    supported_efforts=("low", "high", "max"),
    default_effort="max",
    default=ReasoningRequest(mode="enabled", effort="max"),
    output="text",
    continuation="text",
    variants={
        "enabled": {"reasoning_effort": "max"},
        **{effort: {"reasoning_effort": effort} for effort in ("low", "high", "max")},
    },
)
_GLM_REASONING = toggle_reasoning(
    name="glm-thinking-toggle",
    enabled={"thinking": {"type": "enabled", "clear_thinking": False}},
    disabled={"thinking": {"type": "disabled"}},
)
_GLM_53_REASONING = mapped_reasoning(
    name="glm-5.3-thinking-effort",
    activation="always_on",
    supported_efforts=("low", "high", "max"),
    default_effort="max",
    default=ReasoningRequest(mode="enabled", effort="max"),
    output="text",
    continuation="text",
    variants={
        "enabled": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
        **{
            effort: {
                "thinking": {"type": "enabled"},
                "reasoning_effort": effort,
            }
            for effort in ("low", "high", "max")
        },
    },
)
_GLM_53_FLASH_REASONING = mapped_reasoning(
    name="glm-5.3-flash-thinking-effort",
    activation="always_on",
    supported_efforts=("low", "high", "max"),
    default_effort="max",
    default=ReasoningRequest(mode="enabled", effort="max"),
    output="text",
    continuation="text",
    variants={
        "enabled": {
            "thinking": {"type": "enabled", "clear_thinking": False},
            "reasoning_effort": "max",
        },
        **{
            effort: {
                "thinking": {"type": "enabled", "clear_thinking": False},
                "reasoning_effort": effort,
            }
            for effort in ("low", "high", "max")
        },
    },
)
_GROK_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh"),
    default_effort="high",
    output="summary",
)
_ANTHROPIC_SONNET_REASONING = mapped_reasoning(
    name="anthropic-always-on-adaptive-effort-high",
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default_effort="high",
    default=ReasoningRequest(mode="enabled", effort="high"),
    output="summary",
    continuation="opaque",
    variants={
        "enabled": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}},
        **{
            effort: {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
            for effort in ("low", "medium", "high", "xhigh", "max")
        },
    },
)
_ANTHROPIC_OPUS_REASONING = mapped_reasoning(
    name="anthropic-always-on-adaptive-effort-medium",
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default_effort="medium",
    default=ReasoningRequest(mode="enabled", effort="medium"),
    output="summary",
    continuation="opaque",
    variants={
        "enabled": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}},
        "low": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
        },
        "medium": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"},
        },
        "high": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        },
        "xhigh": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "xhigh"},
        },
        "max": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "max"},
        },
    },
)
_ANTHROPIC_HAIKU_REASONING = mapped_reasoning(
    name="anthropic-adaptive-effort-medium",
    activation="toggle",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default_effort="medium",
    default=ReasoningRequest(mode="enabled", effort="medium"),
    output="summary",
    continuation="opaque",
    variants={
        "disabled": {"thinking": {"type": "disabled"}, "output_config": {"effort": "medium"}},
        "enabled": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}},
        **{
            effort: {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
            for effort in ("low", "medium", "high", "xhigh", "max")
        },
    },
)
_MINIMAX_REASONING = toggle_reasoning(
    name="minimax-adaptive-thinking",
    enabled={"thinking": {"type": "adaptive"}},
    disabled={"thinking": {"type": "disabled"}},
)
_MINIMAX_M2_REASONING = mapped_reasoning(
    name="minimax-always-on-thinking",
    activation="always_on",
    default=ReasoningRequest(mode="enabled"),
    output="text",
    continuation="text",
    variants={"enabled": {}},
)
_DOUBAO_REASONING = mapped_reasoning(
    name="doubao-thinking-effort",
    activation="toggle",
    supported_efforts=("minimal", "low", "medium", "high"),
    default_effort="high",
    default=ReasoningRequest(mode="enabled", effort="high"),
    output="summary",
    continuation="text",
    variants={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        **{
            effort: {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
            for effort in ("minimal", "low", "medium", "high")
        },
    },
)
_COHERE_REASONING = openai_chat_completions_effort_reasoning(
    supported_efforts=("high",),
    default_effort="high",
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


_PROVIDER_TEMPLATES = (
    ProviderTemplate(
        template_id="openai-codex",
        display_name="OpenAI Codex (ChatGPT subscription)",
        description=(
            "Browser subscription login with the documented Codex model catalog. "
            "Availability depends on the account and rollout. "
            "GPT-5.5 retires from Codex on 2026-10-14. "
            "No request output limit; context 1050000 and output 32768 are local planning budgets. "
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
                max_output_tokens=32_768,
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
        revision=7,
    ),
    ProviderTemplate(
        template_id="openai",
        display_name="OpenAI",
        description=(
            "OpenAI Responses API with GPT-6.1 Sol, GPT-6 Astra/Sol/Luna, GPT-5.6 Sol/Terra/Luna "
            "and GPT-5.5. "
            "Documented context 1050000 and output maximum 128000; "
            "the preset requests an output budget of 32768. GPT-6 tool calling requires Responses. "
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
                max_output_tokens=32_768,
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
        revision=6,
    ),
    ProviderTemplate(
        template_id="anthropic",
        display_name="Anthropic",
        description="Anthropic Messages API with current Claude production models.",
        provider_type="anthropic",
        protocol="anthropic-messages",
        credential_env="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com",
        api_path="/v1/messages",
        default_endpoint="claude-sonnet-5-5",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="claude-fable-5-1",
                display_name="Claude Fable 5.1",
                model_id="claude-fable-5-1",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_SONNET_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="claude-sonnet-5-5",
                display_name="Claude Sonnet 5.5",
                model_id="claude-sonnet-5-5",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_SONNET_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="claude-opus-5-5",
                display_name="Claude Opus 5.5",
                model_id="claude-opus-5-5",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_OPUS_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="claude-haiku-5-5",
                display_name="Claude Haiku 5.5",
                model_id="claude-haiku-5-5",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_HAIKU_REASONING,
            ),
        ),
        documentation_url="https://platform.claude.com/docs/en/models/overview",
        verified_on=_VERIFIED_ON,
        revision=4,
    ),
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
        revision=5,
    ),
    ProviderTemplate(
        template_id="deepseek",
        display_name="DeepSeek",
        description=(
            "DeepSeek's Chat Completions API with V4 Pro and V4.1 Flash (deepseek-flash). "
            "Text-only models; the preset requests an output budget of 32768."
        ),
        provider_type="deepseek",
        protocol="openai-chat-completions",
        credential_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        api_path="/chat/completions",
        default_endpoint="deepseek-v4-pro",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="deepseek-v4-pro",
                display_name="DeepSeek V4 Pro",
                model_id="deepseek-v4-pro",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DEEPSEEK_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="deepseek-flash",
                display_name="DeepSeek V4.1 Flash",
                model_id="deepseek-flash",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DEEPSEEK_REASONING,
            ),
        ),
        documentation_url="https://api-docs.deepseek.com/quick_start/pricing/",
        verified_on=_VERIFIED_ON,
        revision=5,
    ),
    ProviderTemplate(
        template_id="qwen",
        display_name="Alibaba Cloud Qwen",
        description=(
            "DashScope's OpenAI Chat Completions-compatible China endpoint with the Qwen3.8-Max "
            "flagship, Qwen3.8-Flash, and Qwen3.7-Plus multimodal models."
        ),
        provider_type="qwen",
        protocol="openai-chat-completions",
        credential_env="DASHSCOPE_API_KEY",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_path="/chat/completions",
        default_endpoint="qwen3.8-max",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="qwen3.8-max",
                display_name="Qwen3.8 Max",
                model_id="qwen3.8-max",
                context_window=1_000_000,
                max_output_tokens=131_072,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_QWEN_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="qwen3.8-flash",
                display_name="Qwen3.8 Flash",
                model_id="qwen3.8-flash",
                context_window=1_000_000,
                max_output_tokens=131_072,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_QWEN_FLASH_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="qwen3.7-plus",
                display_name="Qwen3.7 Plus",
                model_id="qwen3.7-plus",
                context_window=1_000_000,
                max_output_tokens=131_072,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_QWEN_REASONING,
            ),
        ),
        documentation_url="https://help.aliyun.com/zh/model-studio/text-generation-model",
        verified_on=_VERIFIED_ON,
        revision=7,
    ),
    ProviderTemplate(
        template_id="kimi",
        display_name="Moonshot AI Kimi",
        description="Kimi's Chat Completions API with the K3 flagship and K2 Agent models.",
        provider_type="kimi",
        protocol="openai-chat-completions",
        credential_env="MOONSHOT_API_KEY",
        base_url="https://api.moonshot.ai/v1",
        api_path="/chat/completions",
        default_endpoint="kimi-k3",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="kimi-k3",
                display_name="Kimi K3",
                model_id="kimi-k3",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_K3_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.7-code",
                display_name="Kimi K2.7 Code",
                model_id="kimi-k2.7-code",
                context_window=262_144,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_ALWAYS_ON_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.7-code-highspeed",
                display_name="Kimi K2.7 Code Highspeed",
                model_id="kimi-k2.7-code-highspeed",
                context_window=262_144,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_ALWAYS_ON_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.6",
                display_name="Kimi K2.6",
                model_id="kimi-k2.6",
                context_window=262_144,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_REASONING,
            ),
        ),
        documentation_url="https://platform.kimi.com/docs/models",
        verified_on=_VERIFIED_ON,
        revision=4,
    ),
    ProviderTemplate(
        template_id="glm",
        display_name="Zhipu GLM",
        description="Zhipu's Chat Completions API with current text and multimodal models.",
        provider_type="glm",
        protocol="openai-chat-completions",
        credential_env="ZAI_API_KEY",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_path="/chat/completions",
        default_endpoint="glm-5.3",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="glm-5.3",
                display_name="GLM-5.3",
                model_id="glm-5.3",
                context_window=1_000_000,
                max_output_tokens=128_000,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_53_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5.3-flash",
                display_name="GLM-5.3 Flash",
                model_id="glm-5.3-flash",
                context_window=1_000_000,
                max_output_tokens=128_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_53_FLASH_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5.2",
                display_name="GLM-5.2",
                model_id="glm-5.2",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5-turbo",
                display_name="GLM-5 Turbo",
                model_id="glm-5-turbo",
                context_window=200_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5v-turbo",
                display_name="GLM-5V Turbo",
                model_id="glm-5v-turbo",
                context_window=200_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_GLM_REASONING,
            ),
        ),
        documentation_url="https://docs.bigmodel.cn/cn/guide/start/model-overview",
        verified_on=_VERIFIED_ON,
        revision=5,
    ),
    ProviderTemplate(
        template_id="grok",
        display_name="xAI Grok",
        description="xAI's Chat Completions API with the latest Grok frontier model.",
        provider_type="grok",
        protocol="openai-chat-completions",
        credential_env="XAI_API_KEY",
        base_url="https://api.x.ai/v1",
        api_path="/chat/completions",
        default_endpoint="grok-4.7",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="grok-4.7",
                display_name="Grok 4.7",
                model_id="grok-4.7",
                context_window=500_000,
                max_output_tokens=128_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GROK_REASONING,
                metadata={"provider_output_limit": "unlimited"},
            ),
        ),
        documentation_url="https://docs.x.ai/developers/grok-4-7",
        verified_on=_VERIFIED_ON,
        revision=4,
    ),
    ProviderTemplate(
        template_id="mistral",
        display_name="Mistral AI",
        description=(
            "Mistral's multimodal Chat Completions models, including Large 4 Public Preview. "
            "Reasoning is set to none because OURO does not decode Mistral thinking chunks."
        ),
        provider_type="mistral",
        protocol="openai-chat-completions",
        credential_env="MISTRAL_API_KEY",
        base_url="https://api.mistral.ai/v1",
        api_path="/chat/completions",
        default_endpoint="mistral-medium-3-5",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="mistral-medium-3-5",
                display_name="Mistral Medium 3.5",
                model_id="mistral-medium-3-5",
                context_window=256_000,
                max_output_tokens=32_768,
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
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                request_options={"reasoning_effort": "none"},
            ),
            ProviderEndpointConfig(
                endpoint_id="mistral-large-4-0",
                display_name="Mistral Large 4 (Public Preview)",
                model_id="mistral-large-4-0",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                request_options={"reasoning_effort": "none"},
            ),
        ),
        documentation_url="https://docs.mistral.ai/models",
        verified_on=_VERIFIED_ON,
        revision=3,
    ),
    ProviderTemplate(
        template_id="minimax",
        display_name="MiniMax",
        description=(
            "MiniMax's official China Chat Completions endpoint with multimodal M3 and "
            "text-only M2.7 models."
        ),
        provider_type="minimax",
        protocol="openai-chat-completions",
        credential_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.cn/v1",
        api_path="/chat/completions",
        default_endpoint="MiniMax-M3",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="MiniMax-M3",
                display_name="MiniMax M3",
                model_id="MiniMax-M3",
                context_window=1_000_000,
                max_output_tokens=131_072,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_MINIMAX_REASONING,
                request_options={"reasoning_split": True},
            ),
            ProviderEndpointConfig(
                endpoint_id="MiniMax-M2.7",
                display_name="MiniMax M2.7",
                model_id="MiniMax-M2.7",
                context_window=204_800,
                max_output_tokens=65_536,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_MINIMAX_M2_REASONING,
                request_options={"reasoning_split": True},
            ),
            ProviderEndpointConfig(
                endpoint_id="MiniMax-M2.7-highspeed",
                display_name="MiniMax M2.7 Highspeed",
                model_id="MiniMax-M2.7-highspeed",
                context_window=204_800,
                max_output_tokens=65_536,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_MINIMAX_M2_REASONING,
                request_options={"reasoning_split": True},
            ),
        ),
        documentation_url="https://platform.minimax.cn/docs/api-reference/text-openai-api",
        verified_on=_VERIFIED_ON,
    ),
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
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DOUBAO_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="doubao-seed-2-1-lite-260915",
                display_name="Doubao Seed 2.1 Lite",
                model_id="doubao-seed-2-1-lite-260915",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DOUBAO_REASONING,
            ),
        ),
        documentation_url="https://docs.volcengine.com/docs/ark/model-list?lang=zh",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="cohere",
        display_name="Cohere",
        description="Cohere's OpenAI-compatible API with the multilingual Command A+ model.",
        provider_type="cohere",
        protocol="openai-chat-completions",
        credential_env="COHERE_API_KEY",
        base_url="https://api.cohere.ai/compatibility/v1",
        api_path="/chat/completions",
        default_endpoint="command-a-plus-05-2026",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="command-a-plus-05-2026",
                display_name="Command A+",
                model_id="command-a-plus-05-2026",
                context_window=128_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_COHERE_REASONING,
            ),
        ),
        documentation_url="https://docs.cohere.com/docs/compatibility-api",
        verified_on=_VERIFIED_ON,
    ),
)

_TEMPLATES_BY_ID = {template.template_id: template for template in _PROVIDER_TEMPLATES}


def provider_templates() -> tuple[ProviderTemplate, ...]:
    return _PROVIDER_TEMPLATES


def get_provider_template(template_id: str) -> ProviderTemplate:
    normalized = template_id.strip().casefold()
    template = _TEMPLATES_BY_ID.get(normalized)
    if template is None:
        available = ", ".join(sorted(_TEMPLATES_BY_ID))
        raise ConfigError(f"provider template not found: {template_id}; available: {available}")
    return template
