from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from ouro.config.credentials import CredentialRef
from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_headers import ProviderHeaderValue, parse_provider_headers
from ouro.config.reasoning import EndpointReasoningConfig, ReasoningProfileConfig
from ouro.core.base import Metadata, MetadataField, OuroModel, new_id
from ouro.core.content import MediaModality, input_modalities
from ouro.core.provider import ProviderRetryPolicy
from ouro.core.reasoning import ReasoningContinuation, ReasoningRequest

ProviderType = Literal[
    "faux",
    "openai",
    "openai-codex",
    "anthropic",
    "gemini",
    "deepseek",
    "qwen",
    "kimi",
    "glm",
    "grok",
    "mistral",
    "minimax",
    "doubao",
    "cohere",
    "custom",
]
ProviderProtocol = Literal[
    "faux",
    "openai-chat-completions",
    "openai-responses",
    "anthropic-messages",
]
ProviderTemplateOrigin = Literal["inline", "custom"]

INTEGRATED_PROVIDER_TYPES: tuple[ProviderType, ...] = (
    "openai",
    "openai-codex",
    "anthropic",
    "gemini",
    "deepseek",
    "qwen",
    "kimi",
    "glm",
    "grok",
    "mistral",
    "minimax",
    "doubao",
    "cohere",
)
CONFIGURABLE_PROVIDER_TYPES: tuple[ProviderType, ...] = (
    *INTEGRATED_PROVIDER_TYPES,
    "custom",
)
SUPPORTED_PROVIDER_TYPES: tuple[ProviderType, ...] = (
    "faux",
    *CONFIGURABLE_PROVIDER_TYPES,
)
SUPPORTED_PROVIDER_PROTOCOLS: tuple[ProviderProtocol, ...] = (
    "faux",
    "openai-chat-completions",
    "openai-responses",
    "anthropic-messages",
)
BUILTIN_FAUX_PROVIDER_ID = "faux"


@dataclass(frozen=True)
class ProviderConnectionSpec:
    """One supported protocol/authentication pair and its trusted service rules."""

    protocol: ProviderProtocol
    auth_driver: str = "static"
    service_policy: str | None = None
    notice: str | None = None


@dataclass(frozen=True)
class ProviderTypeSpec:
    """Supported connections, optionally with a product-specific default."""

    provider_type: ProviderType
    default_connection: ProviderConnectionSpec | None
    alternative_connections: tuple[ProviderConnectionSpec, ...] = ()

    def __post_init__(self) -> None:
        connections = self.connections
        if not connections:
            raise ValueError("provider types require at least one connection")
        pairs = [(item.protocol, item.auth_driver) for item in connections]
        if len(set(pairs)) != len(pairs):
            raise ValueError("provider connection pairs must be unique")
        if any(item.auth_driver != "static" and not item.service_policy for item in connections):
            raise ValueError("managed provider connections require a service policy")

    @property
    def connections(self) -> tuple[ProviderConnectionSpec, ...]:
        if self.default_connection is None:
            return self.alternative_connections
        return (self.default_connection, *self.alternative_connections)


@dataclass(frozen=True)
class ProviderProtocolSpec:
    """Connection requirements and input encodings implemented by one registered adapter."""

    protocol: ProviderProtocol
    default_api_path: str | None
    requires_base_url: bool
    reasoning_continuations: tuple[ReasoningContinuation, ...]
    encodable_input_modalities: frozenset[MediaModality]


_OPENAI_CHAT_COMPLETIONS_STATIC = ProviderConnectionSpec("openai-chat-completions")
_ANTHROPIC_MESSAGES_STATIC = ProviderConnectionSpec("anthropic-messages")

_PROVIDER_TYPE_SPECS: dict[ProviderType, ProviderTypeSpec] = {
    "faux": ProviderTypeSpec("faux", ProviderConnectionSpec("faux")),
    "openai": ProviderTypeSpec(
        "openai",
        ProviderConnectionSpec(
            "openai-responses",
            notice=(
                "Persisted Responses state cannot be replayed after restarting this API Key "
                "client; start a new conversation."
            ),
        ),
        alternative_connections=(_OPENAI_CHAT_COMPLETIONS_STATIC,),
    ),
    "openai-codex": ProviderTypeSpec(
        "openai-codex",
        ProviderConnectionSpec(
            "openai-responses",
            "openai-codex",
            "openai-codex",
            notice=(
                "ChatGPT subscription compatibility integration; browser login required. "
                "max_output_tokens is a local planning budget, not a request output limit. "
                "Actual output may exceed this budget."
            ),
        ),
    ),
    "anthropic": ProviderTypeSpec("anthropic", _ANTHROPIC_MESSAGES_STATIC),
    "gemini": ProviderTypeSpec("gemini", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "deepseek": ProviderTypeSpec("deepseek", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "qwen": ProviderTypeSpec("qwen", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "kimi": ProviderTypeSpec("kimi", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "glm": ProviderTypeSpec("glm", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "grok": ProviderTypeSpec("grok", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "mistral": ProviderTypeSpec("mistral", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "minimax": ProviderTypeSpec("minimax", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "doubao": ProviderTypeSpec("doubao", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "cohere": ProviderTypeSpec("cohere", _OPENAI_CHAT_COMPLETIONS_STATIC),
    "custom": ProviderTypeSpec(
        "custom",
        None,
        alternative_connections=(
            _OPENAI_CHAT_COMPLETIONS_STATIC,
            ProviderConnectionSpec("openai-responses"),
            _ANTHROPIC_MESSAGES_STATIC,
        ),
    ),
}

_PROVIDER_PROTOCOL_SPECS: dict[ProviderProtocol, ProviderProtocolSpec] = {
    "openai-responses": ProviderProtocolSpec(
        protocol="openai-responses",
        default_api_path="/responses",
        requires_base_url=True,
        reasoning_continuations=("none", "opaque"),
        encodable_input_modalities=frozenset({"image"}),
    ),
    "faux": ProviderProtocolSpec(
        protocol="faux",
        default_api_path=None,
        requires_base_url=False,
        reasoning_continuations=("none",),
        encodable_input_modalities=frozenset(),
    ),
    "openai-chat-completions": ProviderProtocolSpec(
        protocol="openai-chat-completions",
        default_api_path="/chat/completions",
        requires_base_url=True,
        reasoning_continuations=("none", "text"),
        encodable_input_modalities=frozenset({"image", "audio", "video"}),
    ),
    "anthropic-messages": ProviderProtocolSpec(
        protocol="anthropic-messages",
        default_api_path="/v1/messages",
        requires_base_url=True,
        reasoning_continuations=("none", "opaque"),
        encodable_input_modalities=frozenset({"image"}),
    ),
}

_PROTOCOL_OWNED_REQUEST_FIELDS: dict[ProviderProtocol, frozenset[str]] = {
    "openai-responses": frozenset(
        {
            "model",
            "input",
            "instructions",
            "tools",
            "stream",
            "store",
            "include",
            "temperature",
            "max_output_tokens",
            "previous_response_id",
            "conversation",
            "truncation",
            "background",
        }
    ),
    "faux": frozenset(),
    "openai-chat-completions": frozenset(
        {
            "max_tokens",
            "messages",
            "model",
            "stream",
            "temperature",
            "tools",
        }
    ),
    "anthropic-messages": frozenset(
        {
            "max_tokens",
            "messages",
            "model",
            "stream",
            "system",
            "temperature",
            "tools",
        }
    ),
}


def validate_media_protocol_compatibility(
    protocol: ProviderProtocol,
    *,
    supports_image_input: bool,
    supports_audio_input: bool,
    supports_video_input: bool,
) -> None:
    declared = input_modalities(
        supports_image_input=supports_image_input,
        supports_audio_input=supports_audio_input,
        supports_video_input=supports_video_input,
    )
    unsupported = declared - provider_protocol_spec(protocol).encodable_input_modalities
    if unsupported:
        kinds = ", ".join(sorted(unsupported))
        raise ValueError(f"{protocol} adapter does not support {kinds} input encoding")


def provider_type_spec(provider_type: ProviderType) -> ProviderTypeSpec:
    try:
        return _PROVIDER_TYPE_SPECS[provider_type]
    except KeyError as exc:
        raise ValueError(f"unsupported provider type: {provider_type}") from exc


def provider_protocol_spec(protocol: ProviderProtocol) -> ProviderProtocolSpec:
    return _PROVIDER_PROTOCOL_SPECS[protocol]


def resolve_provider_connection(
    provider_type: ProviderType,
    protocol: ProviderProtocol | None = None,
    auth_driver: str | None = None,
) -> ProviderConnectionSpec:
    """Resolve defaults, then validate the pair without a Cartesian product."""
    spec = provider_type_spec(provider_type)
    default = spec.default_connection
    if protocol is None:
        if default is None:
            raise ValueError(f"protocol is required for provider type {provider_type!r}")
        protocol = default.protocol
    selected_driver = (
        auth_driver if auth_driver is not None else (default.auth_driver if default else "static")
    )
    for connection in spec.connections:
        if (connection.protocol, connection.auth_driver) == (protocol, selected_driver):
            return connection
    raise ValueError(
        f"protocol/auth_driver combination is not supported by provider type {provider_type!r}"
    )


def validate_credential_binding(auth_driver: str, ref: CredentialRef) -> None:
    if auth_driver == "static" and ref.type == "managed":
        raise ValueError("static authentication cannot use a managed credential")
    if auth_driver != "static" and ref.type != "managed":
        raise ValueError("managed authentication requires a managed credential")


def default_api_path(protocol: ProviderProtocol) -> str | None:
    return provider_protocol_spec(protocol).default_api_path


def validate_request_options(
    protocol: ProviderProtocol,
    request_options: dict[str, Any],
    *,
    supports_structured_output: bool | None = None,
) -> None:
    """Reject request fields owned by the protocol adapter or endpoint contract."""

    if protocol == "faux" and request_options:
        raise ValueError("faux protocol does not accept request options")
    collisions = sorted(_PROTOCOL_OWNED_REQUEST_FIELDS[protocol].intersection(request_options))
    if collisions:
        raise ValueError(
            "request options conflict with protocol-owned fields: " + ", ".join(collisions)
        )
    if (
        protocol == "openai-chat-completions"
        and supports_structured_output is False
        and "response_format" in request_options
    ):
        raise ValueError("response_format requires an endpoint with structured output support")
    if protocol == "openai-responses":
        text = request_options.get("text")
        if text is not None and not isinstance(text, dict):
            raise ValueError("Responses text options must be an object")
        if isinstance(text, dict) and "format" in text and supports_structured_output is False:
            raise ValueError("text.format requires an endpoint with structured output support")


def validate_reasoning_protocol_compatibility(
    protocol: ProviderProtocol,
    reasoning: EndpointReasoningConfig,
) -> None:
    """Reject continuation contracts a wire adapter cannot faithfully replay."""

    capability = reasoning.capability
    spec = provider_protocol_spec(protocol)
    if capability.continuation not in spec.reasoning_continuations:
        raise ValueError(
            f"reasoning continuation {capability.continuation!r} is not supported by "
            f"protocol {protocol!r}"
        )
    if protocol == "faux" and capability.activation != "unsupported":
        raise ValueError("faux protocol does not support reasoning controls")
    if protocol == "openai-responses" and reasoning.profile is not None:
        validate_responses_reasoning_profile(reasoning.profile)


def validate_responses_reasoning_profile(profile: ReasoningProfileConfig) -> None:
    """Responses controls use one native object, never Chat Completions fields."""
    if not profile.owned_fields <= {"reasoning"}:
        raise ValueError("Responses reasoning profiles must use the reasoning object")
    if any(
        not isinstance(fragment.get("reasoning", {}), dict)
        for fragment in profile.variants.values()
    ):
        raise ValueError("Responses reasoning controls must be an object")


class ProviderTemplateEndpointBinding(OuroModel):
    """Last template baseline for one configured endpoint identity."""

    template_endpoint_id: str
    configured_endpoint_id: str
    fingerprint: str

    @field_validator("template_endpoint_id", "configured_endpoint_id", "fingerprint")
    @classmethod
    def _require_binding_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("template endpoint binding values must not be empty")
        return normalized


class ProviderTemplateBinding(OuroModel):
    """Provenance and compact baseline needed for safe explicit template updates."""

    template_id: str
    origin: ProviderTemplateOrigin
    revision: int = Field(ge=1)
    auth_driver: str = "static"
    credential_env: str | None = None
    base_url: str
    api_path: str
    default_endpoint: str
    endpoints: tuple[ProviderTemplateEndpointBinding, ...] = Field(default_factory=tuple)

    @field_validator("template_id", "auth_driver", "base_url", "api_path", "default_endpoint")
    @classmethod
    def _require_template_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("template binding values must not be empty")
        return normalized

    @field_validator("credential_env")
    @classmethod
    def _validate_credential_env(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._require_template_value(value)

    @model_validator(mode="after")
    def _validate_endpoint_bindings(self) -> ProviderTemplateBinding:
        template_ids = [item.template_endpoint_id for item in self.endpoints]
        configured_ids = [item.configured_endpoint_id for item in self.endpoints]
        if len(set(template_ids)) != len(template_ids):
            raise ValueError("template endpoint binding ids must be unique")
        if len(set(configured_ids)) != len(configured_ids):
            raise ValueError("configured endpoint binding ids must be unique")
        return self

    def endpoint_binding(
        self,
        template_endpoint_id: str,
    ) -> ProviderTemplateEndpointBinding | None:
        return next(
            (item for item in self.endpoints if item.template_endpoint_id == template_endpoint_id),
            None,
        )


class ProviderConfig(OuroModel):
    """Supplier-level product identity, protocol, connection, and endpoints."""

    model_config = ConfigDict(hide_input_in_errors=True)

    provider_id: str = Field(default_factory=lambda: new_id("provider"))
    display_name: str
    provider_type: ProviderType
    protocol: ProviderProtocol
    auth_driver: str = "static"
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)
    default_endpoint: str | None = None
    endpoints: tuple[ProviderEndpointConfig, ...] = Field(default_factory=tuple)
    base_url: str | None = None
    api_path: str | None = None
    enabled: bool = True
    timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    retry_policy: ProviderRetryPolicy = Field(default_factory=ProviderRetryPolicy)
    headers: dict[str, ProviderHeaderValue] = Field(default_factory=dict, repr=False)
    extra: Metadata = MetadataField
    template: ProviderTemplateBinding | None = None

    _validate_headers = field_validator("headers", mode="before")(parse_provider_headers)

    @field_validator("provider_id", "display_name")
    @classmethod
    def _require_provider_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        return normalized or None

    @field_validator("api_path")
    @classmethod
    def _normalize_api_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized if normalized.startswith("/") else f"/{normalized}"

    @field_validator("credential_ref", mode="before")
    @classmethod
    def _parse_credential_ref(cls, value: object) -> object:
        if isinstance(value, str) or value is None:
            return CredentialRef.parse(value)
        return value

    @model_validator(mode="after")
    def _validate_provider(self) -> ProviderConfig:
        if self.protocol == "faux" and self.headers:
            raise ValueError("faux protocol does not accept HTTP headers")
        resolve_provider_connection(self.provider_type, self.protocol, self.auth_driver)
        validate_credential_binding(self.auth_driver, self.credential_ref)
        validate_request_options(self.protocol, self.extra)
        for endpoint in self.endpoints:
            validate_media_protocol_compatibility(
                self.protocol,
                supports_image_input=endpoint.supports_image_input,
                supports_audio_input=endpoint.supports_audio_input,
                supports_video_input=endpoint.supports_video_input,
            )
            validate_reasoning_protocol_compatibility(self.protocol, endpoint.reasoning)
            request_options = {**self.extra, **endpoint.request_options}
            validate_request_options(
                self.protocol,
                request_options,
                supports_structured_output=endpoint.supports_structured_output,
            )
            endpoint.reasoning.validate_request_options(request_options)
        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if len(set(endpoint_ids)) != len(endpoint_ids):
            raise ValueError("endpoint ids must be unique")
        if self.default_endpoint is not None and self.default_endpoint not in endpoint_ids:
            raise ValueError(f"default endpoint not found: {self.default_endpoint}")
        if provider_protocol_spec(self.protocol).requires_base_url:
            missing_base_url = next(
                (
                    endpoint.endpoint_id
                    for endpoint in self.endpoints
                    if not (endpoint.base_url or self.base_url)
                ),
                None,
            )
            if missing_base_url is not None:
                raise ValueError(
                    f"base_url is required for endpoint: {self.provider_id}/{missing_base_url}"
                )
        return self

    def endpoint(self, endpoint_id: str | None = None) -> ProviderEndpointConfig | None:
        target = endpoint_id or self.default_endpoint
        if target is None:
            return None
        return next(
            (endpoint for endpoint in self.endpoints if endpoint.endpoint_id == target),
            None,
        )


class ProviderTemplateUpdateResult(OuroModel):
    """Typed, user-facing facts produced by one atomic template update."""

    provider: ProviderConfig
    template_id: str
    from_revision: int
    to_revision: int
    changed: bool = False
    added_endpoints: tuple[str, ...] = ()
    updated_endpoints: tuple[str, ...] = ()
    removed_endpoints: tuple[str, ...] = ()
    preserved_endpoints: tuple[str, ...] = ()
    conflicted_endpoints: tuple[str, ...] = ()
    updated_fields: tuple[str, ...] = ()
    preserved_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderRuntimeOptions:
    protocol: ProviderProtocol | None = None
    auth_driver: str | None = None
    service_policy: str | None = None
    provider_id: str | None = None
    endpoint_id: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_path: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    credential_ref: CredentialRef | None = None
    timeout: float | None = None
    temperature: float | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_image_input: bool | None = None
    supports_audio_input: bool | None = None
    supports_video_input: bool | None = None
    supports_streaming: bool | None = None
    supports_structured_output: bool | None = None
    reasoning: EndpointReasoningConfig | None = None
    reasoning_request: ReasoningRequest | None = None
    retry_policy: ProviderRetryPolicy | None = None
    headers: dict[str, ProviderHeaderValue] | None = field(default=None, repr=False)
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderConfigSnapshot:
    default_provider_id: str
    providers: tuple[ProviderConfig, ...]
    config_path: Path


def normalize_provider_config(provider: ProviderConfig) -> ProviderConfig:
    """Install built-in provider defaults without performing persistence work."""

    if provider.protocol == "faux" and not provider.endpoints:
        endpoint = ProviderEndpointConfig(
            endpoint_id="faux",
            display_name="Faux",
            model_id="faux",
            supports_tools=True,
        )
        return ProviderConfig.model_validate(
            provider.model_copy(
                update={
                    "default_endpoint": endpoint.endpoint_id,
                    "endpoints": (endpoint,),
                }
            ).model_dump()
        )
    return provider


def builtin_faux_provider() -> ProviderConfig:
    endpoint = ProviderEndpointConfig(
        endpoint_id="faux",
        display_name="Faux",
        model_id="faux",
        supports_tools=True,
    )
    return ProviderConfig(
        provider_id=BUILTIN_FAUX_PROVIDER_ID,
        display_name="Faux",
        provider_type="faux",
        protocol="faux",
        default_endpoint=endpoint.endpoint_id,
        endpoints=(endpoint,),
        enabled=True,
        timeout_seconds=60.0,
    )
