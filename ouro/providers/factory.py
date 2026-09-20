from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ouro.auth.request_auth import RequestAuthenticator
from ouro.config.credentials import CredentialRef, CredentialResolver
from ouro.config.provider_models import (
    SUPPORTED_PROVIDER_PROTOCOLS,
    ProviderProtocol,
    ProviderRuntimeOptions,
    default_api_path,
    provider_protocol_spec,
    validate_reasoning_protocol_compatibility,
    validate_request_options,
)
from ouro.config.provider_runtime import validate_runtime_auth
from ouro.config.reasoning import EndpointReasoningConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.billing import BillingMode
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningRequest
from ouro.providers.anthropic_messages import AnthropicMessagesProvider
from ouro.providers.authentication import ProviderRequestPolicy
from ouro.providers.faux import FauxProvider
from ouro.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from ouro.providers.openai_responses import OpenAIResponsesProvider
from ouro.providers.reasoning import ReasoningPayloadEncoder


@dataclass(frozen=True)
class _AdapterBuildContext:
    options: ProviderRuntimeOptions
    model: ModelMetadata
    provider_id: str
    base_url: str | None
    api_key: str | None
    timeout: float
    authenticator: RequestAuthenticator | None
    request_policy: ProviderRequestPolicy | None


AdapterBuilder = Callable[[_AdapterBuildContext], RuntimeAdapter]


def build_runtime_adapter(
    options: ProviderRuntimeOptions,
    *,
    credentials: CredentialResolver | None = None,
    authenticator: RequestAuthenticator | None = None,
    request_policy: ProviderRequestPolicy | None = None,
) -> RuntimeAdapter:
    """Build the provider boundary from fully resolved runtime options."""

    validate_runtime_auth(options)
    if options.auth_driver not in (None, "static") and authenticator is None:
        raise ConfigError("managed request authenticator must be supplied by composition")
    protocol = options.protocol or "faux"
    if protocol == "faux" and options.headers:
        raise ConfigError("faux protocol does not accept HTTP headers")
    if protocol not in SUPPORTED_PROVIDER_PROTOCOLS:
        raise ConfigError(f"unsupported provider protocol: {protocol}")
    default_provider_id = "faux" if protocol == "faux" else protocol
    provider_id = _normalized_identifier(
        options.provider_id,
        default=default_provider_id,
        option="--provider",
    )
    assert provider_id is not None
    base_url = options.base_url.strip().rstrip("/") if options.base_url is not None else None
    protocol_spec = provider_protocol_spec(protocol)
    if protocol_spec.requires_base_url and not base_url:
        raise ConfigError(f"--base-url is required for {protocol} protocol")
    model_id = _normalized_identifier(
        options.model,
        default="faux" if protocol == "faux" else None,
        option="--endpoint model",
    )
    if model_id is None:
        raise ConfigError(f"--model is required for {protocol} protocol")

    timeout = 60.0 if options.timeout is None else options.timeout
    if timeout <= 0:
        raise ConfigError("timeout must be greater than 0")

    api_key = None
    if protocol != "faux" and authenticator is None:
        resolver = credentials or CredentialResolver()
        api_key = resolver.resolve(_credential_ref(options))
    context = _AdapterBuildContext(
        options=options,
        model=_model_metadata(
            options,
            protocol=protocol,
            provider_id=provider_id,
            model_id=model_id,
            billing_mode=request_policy.billing_mode if request_policy is not None else "unknown",
        ),
        provider_id=provider_id,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        authenticator=authenticator,
        request_policy=request_policy,
    )
    builder = _ADAPTER_BUILDERS.get(protocol)
    if builder is None:
        raise ConfigError(f"provider protocol adapter is not registered: {protocol}")
    return builder(context)


def _build_faux(context: _AdapterBuildContext) -> RuntimeAdapter:
    return RuntimeAdapter(FauxProvider(), context.model)


def _build_openai_chat_completions(context: _AdapterBuildContext) -> RuntimeAdapter:
    assert context.base_url is not None
    api_path = context.options.api_path or default_api_path("openai-chat-completions")
    assert api_path is not None
    provider = OpenAIChatCompletionsProvider(
        base_url=context.base_url,
        api_key=context.api_key,
        authenticator=context.authenticator,
        request_policy=context.request_policy,
        provider_id=context.provider_id,
        api_path=api_path,
        timeout=context.timeout,
        headers=context.options.headers,
        request_options=context.options.extra,
        reasoning_encoder=ReasoningPayloadEncoder(
            context.options.reasoning.profile if context.options.reasoning is not None else None
        ),
        retry_policy=context.options.retry_policy,
    )
    return RuntimeAdapter(provider, context.model)


def _build_anthropic_messages(context: _AdapterBuildContext) -> RuntimeAdapter:
    assert context.base_url is not None
    api_path = context.options.api_path or default_api_path("anthropic-messages")
    assert api_path is not None
    provider = AnthropicMessagesProvider(
        base_url=context.base_url,
        api_key=context.api_key,
        authenticator=context.authenticator,
        request_policy=context.request_policy,
        provider_id=context.provider_id,
        api_path=api_path,
        timeout=context.timeout,
        headers=context.options.headers,
        request_options=context.options.extra,
        reasoning_encoder=ReasoningPayloadEncoder(
            context.options.reasoning.profile if context.options.reasoning is not None else None
        ),
        retry_policy=context.options.retry_policy,
    )
    return RuntimeAdapter(provider, context.model)


def _build_openai_responses(context: _AdapterBuildContext) -> RuntimeAdapter:
    assert context.base_url is not None
    api_path = context.options.api_path or default_api_path("openai-responses")
    assert api_path is not None
    provider = OpenAIResponsesProvider(
        base_url=context.base_url,
        api_key=context.api_key,
        authenticator=context.authenticator,
        request_policy=context.request_policy,
        provider_id=context.provider_id,
        api_path=api_path,
        timeout=context.timeout,
        headers=context.options.headers,
        request_options=context.options.extra,
        reasoning_encoder=ReasoningPayloadEncoder(
            context.options.reasoning.profile if context.options.reasoning is not None else None
        ),
        retry_policy=context.options.retry_policy,
    )
    return RuntimeAdapter(provider, context.model)


_ADAPTER_BUILDERS: dict[ProviderProtocol, AdapterBuilder] = {
    "faux": _build_faux,
    "openai-chat-completions": _build_openai_chat_completions,
    "openai-responses": _build_openai_responses,
    "anthropic-messages": _build_anthropic_messages,
}


def _model_metadata(
    options: ProviderRuntimeOptions,
    *,
    protocol: ProviderProtocol,
    provider_id: str,
    model_id: str,
    billing_mode: BillingMode,
) -> ModelMetadata:
    context_window = 128_000 if options.context_window is None else options.context_window
    max_output_tokens = 8192 if options.max_output_tokens is None else options.max_output_tokens
    if context_window < 1:
        raise ConfigError("context_window must be at least 1")
    if max_output_tokens < 1 or max_output_tokens > context_window:
        raise ConfigError("max_output_tokens must be between 1 and context_window")
    if options.temperature is not None and not 0.0 <= options.temperature <= 2.0:
        raise ConfigError("temperature must be between 0 and 2")

    supports_structured_output = (
        False if options.supports_structured_output is None else options.supports_structured_output
    )
    reasoning = options.reasoning or EndpointReasoningConfig()
    try:
        validate_request_options(
            protocol,
            options.extra or {},
            supports_structured_output=supports_structured_output,
        )
        reasoning.validate_request_options(options.extra or {})
        validate_reasoning_protocol_compatibility(protocol, reasoning)
    except ValueError as exc:
        raise ConfigError(f"invalid endpoint reasoning config: {exc}") from exc

    try:
        return ModelMetadata(
            model_id=model_id,
            billing_mode=billing_mode,
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
            supports_structured_output=supports_structured_output,
            reasoning=reasoning.capability,
            reasoning_request=options.reasoning_request or ReasoningRequest(),
        )
    except ValueError as exc:
        raise ConfigError(f"invalid model metadata: {exc}") from exc


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
