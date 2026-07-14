"""Compatibility names for the provider-neutral HTTP transport."""

from ouro.providers.http_transport import (
    HttpxProviderHTTPTransport,
    ProviderHTTPTransport,
    ProviderTransportError,
)

HttpxOpenAICompatibleTransport = HttpxProviderHTTPTransport
OpenAICompatibleTransport = ProviderHTTPTransport
OpenAITransportError = ProviderTransportError

__all__ = [
    "HttpxOpenAICompatibleTransport",
    "OpenAICompatibleTransport",
    "OpenAITransportError",
]
