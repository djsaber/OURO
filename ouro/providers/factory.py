from __future__ import annotations

from ouro.config.credentials import CredentialRef, CredentialResolver
from ouro.config.provider_models import ProviderRuntimeOptions, default_api_path
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata
from ouro.providers.faux import FauxProvider
from ouro.providers.openai_compatible import OpenAICompatibleProvider


def build_runtime_adapter(
    options: ProviderRuntimeOptions,
    *,
    credentials: CredentialResolver | None = None,
) -> RuntimeAdapter:
    """Build the provider boundary from fully resolved runtime options."""

    provider_kind = options.provider or "faux"
    if provider_kind not in {"faux", "openai-compatible"}:
        raise ConfigError(f"unsupported provider: {provider_kind}")
    default_provider_id = "faux" if provider_kind == "faux" else "openai-compatible"
    provider_id = _normalized_identifier(
        options.provider_id,
        default=default_provider_id,
        option="--provider",
    )
    assert provider_id is not None
    base_url = options.base_url.strip().rstrip("/") if options.base_url is not None else None
    if provider_kind == "openai-compatible" and not base_url:
        raise ConfigError("--base-url is required for openai-compatible provider")
    model_id = _normalized_identifier(
        options.model,
        default="faux" if provider_kind == "faux" else None,
        option="--endpoint model",
    )
    if model_id is None:
        raise ConfigError("--model is required for openai-compatible provider")

    timeout = 60.0 if options.timeout is None else options.timeout
    if timeout <= 0:
        raise ConfigError("timeout must be greater than 0")

    model = _model_metadata(options, provider_id=provider_id, model_id=model_id)
    if provider_kind == "faux":
        return RuntimeAdapter(FauxProvider(), model)

    if provider_kind != "openai-compatible":
        raise ConfigError(f"unsupported provider: {provider_kind}")
    assert base_url is not None
    resolver = credentials or CredentialResolver()
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        api_key=resolver.resolve(_credential_ref(options)),
        provider_id=provider_id,
        endpoint=options.api_path or default_api_path(provider_kind) or "/chat/completions",
        timeout=timeout,
        request_options=options.extra,
        retry_policy=options.retry_policy,
    )
    return RuntimeAdapter(provider, model)


def _model_metadata(
    options: ProviderRuntimeOptions,
    *,
    provider_id: str,
    model_id: str,
) -> ModelMetadata:
    context_window = 128_000 if options.context_window is None else options.context_window
    max_output_tokens = 8192 if options.max_output_tokens is None else options.max_output_tokens
    if context_window < 1:
        raise ConfigError("context_window must be at least 1")
    if max_output_tokens < 1 or max_output_tokens > context_window:
        raise ConfigError("max_output_tokens must be between 1 and context_window")
    if options.temperature is not None and not 0.0 <= options.temperature <= 2.0:
        raise ConfigError("temperature must be between 0 and 2")

    supports_reasoning = options.supports_reasoning or False
    thinking_levels = options.supported_thinking_levels or ()
    thinking_level = options.thinking_level
    if len(set(thinking_levels)) != len(thinking_levels):
        raise ConfigError("supported_thinking_levels must not contain duplicates")
    if not supports_reasoning and thinking_levels:
        raise ConfigError("supported_thinking_levels require supports_reasoning=true")
    if not supports_reasoning and thinking_level not in {None, "off"}:
        raise ConfigError("thinking_level requires supports_reasoning=true")
    if thinking_level is not None and thinking_levels and thinking_level not in thinking_levels:
        raise ConfigError(f"thinking level {thinking_level!r} is not supported by endpoint")

    return ModelMetadata(
        model_id=model_id,
        provider_id=provider_id,
        endpoint_id=options.endpoint_id,
        display_name=model_id,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        temperature=options.temperature,
        supports_tools=True if options.supports_tools is None else options.supports_tools,
        supports_vision=False if options.supports_vision is None else options.supports_vision,
        supports_streaming=True
        if options.supports_streaming is None
        else options.supports_streaming,
        supports_structured_output=False
        if options.supports_structured_output is None
        else options.supports_structured_output,
        supports_reasoning=supports_reasoning,
        supported_thinking_levels=thinking_levels,
        default_thinking_level=thinking_level,
    )


def _credential_ref(options: ProviderRuntimeOptions) -> CredentialRef:
    if options.api_key_env:
        return CredentialRef.parse(f"env:{options.api_key_env}")
    if options.api_key is not None:
        return CredentialRef.parse(options.api_key)
    if options.credential_ref is not None:
        return options.credential_ref
    return CredentialRef()


def _normalized_identifier(
    value: str | None,
    *,
    default: str | None,
    option: str,
) -> str | None:
    if value is None:
        return default
    normalized = value.strip()
    if not normalized:
        raise ConfigError(f"{option} must not be empty")
    return normalized
