"""Compose protocol plugins with native model configuration and header resolution."""

from __future__ import annotations

from copy import deepcopy

from ouro.core.provider import Provider
from ouro.core.request_context import ProviderRequestContext
from ouro.plugins.providers import PluginProvider, ProviderBinder
from ouro.providers.factory import ProviderBuildContext
from ouro.providers.headers import ProviderHeaderResolver
from ouro.sdk.plugins.providers import ProviderConnection, ProviderRetryPolicy


def build_plugin_provider(context: ProviderBuildContext, *, bind: ProviderBinder) -> Provider:
    options = context.options
    protocol = options.protocol
    assert protocol is not None
    headers = ProviderHeaderResolver(options.headers)

    def connection(request: ProviderRequestContext | None) -> ProviderConnection:
        return ProviderConnection(
            protocol=protocol,
            base_url=context.base_url,
            api_path=options.api_path,
            api_key=context.api_key,
            headers=headers.resolve(request, defaults={}),
            timeout_seconds=context.timeout,
            retry=ProviderRetryPolicy(**options.retry_policy.model_dump())
            if options.retry_policy is not None
            else ProviderRetryPolicy(),
            request_options=deepcopy(options.extra or {}),
            reasoning_profile=options.reasoning.profile.model_dump(mode="json")
            if options.reasoning is not None and options.reasoning.profile is not None
            else None,
        )

    return PluginProvider(
        context.provider_id,
        protocol,
        bind,
        connection,
        sensitive_headers=frozenset(
            name.lower()
            for name, value in (options.headers or {}).items()
            if value.source != "context"
        ),
    )
