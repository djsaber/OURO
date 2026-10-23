from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event

from ouro.auth.contracts import AuthError, CommitGuard
from ouro.config.file import ConfigMutationTimeout
from ouro.config.provider_models import ProviderConfig, resolve_provider_connection
from ouro.config.providers.manager import ProviderConfigManager
from ouro.core.errors import ConfigError


@dataclass(frozen=True)
class ProviderAuthBinding:
    """Only facts that bind an authorization to its configured service targets."""

    provider_id: str
    provider_type: str
    protocol: str
    driver_id: str
    credential_id: str | None
    service_policy: str | None
    base_url: str | None
    api_path: str | None
    endpoint_targets: tuple[tuple[str, str | None, str | None], ...]

    @classmethod
    def from_provider(cls, provider: ProviderConfig) -> ProviderAuthBinding:
        connection = resolve_provider_connection(
            provider.provider_type, provider.protocol, provider.auth_driver
        )
        return cls(
            provider_id=provider.provider_id,
            provider_type=provider.provider_type,
            protocol=provider.protocol,
            driver_id=provider.auth_driver,
            credential_id=provider.credential_ref.name
            if provider.credential_ref.type == "managed"
            else None,
            service_policy=connection.service_policy,
            base_url=provider.base_url,
            api_path=provider.api_path,
            endpoint_targets=tuple(
                sorted(
                    (endpoint.endpoint_id, endpoint.base_url, endpoint.api_path)
                    for endpoint in provider.endpoints
                    if endpoint.base_url is not None or endpoint.api_path is not None
                )
            ),
        )


def binding_guard(config: ProviderConfigManager, expected: ProviderAuthBinding) -> CommitGuard:
    @contextmanager
    def guard(*, timeout: float, cancel_event: Event) -> Iterator[None]:
        # Config owns its lock/schema; Application owns the comparison. Auth
        # executes this entire boundary in the same worker as credential save.
        try:
            with config.mutation(timeout=timeout, cancel_event=cancel_event):
                current = config.get_provider(expected.provider_id)
                if current is None or ProviderAuthBinding.from_provider(current) != expected:
                    raise AuthError("credential_conflict")
                yield
        except ConfigMutationTimeout:
            raise AuthError("credential_lock_timeout") from None
        except ConfigError:
            # Config errors can contain raw invalid input. Do not propagate it
            # into an authentication operation's public diagnostic.
            raise AuthError("authorization_configuration_error") from None

    return guard
