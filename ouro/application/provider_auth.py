from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ouro.application.provider_auth_bindings import ProviderAuthBinding, binding_guard
from ouro.application.provider_auth_models import (
    CredentialView,
    LoginChallenge,
    LoginOperation,
    OperationState,
    ProviderAuthState,
    ProviderAuthStatus,
)
from ouro.auth.contracts import AuthError, CredentialSummary
from ouro.auth.credentials import CredentialService
from ouro.auth.driver_contracts import AuthDriver, AuthorizationAttempt, AuthorizationMethod
from ouro.config.providers import ProviderConfigManager
from ouro.core.base import new_id, utc_now
from ouro.core.errors import ConfigError


@dataclass(frozen=True)
class _Operation:
    provider_id: str
    task: asyncio.Task[CredentialSummary]


class ProviderAuthService:
    """Own transient logins; borrow configured drivers and the credential service.

    The composition owner must close this service before closing credentials.
    No constructor reads secrets, starts network I/O, or resolves a default model.
    """

    def __init__(
        self,
        config: ProviderConfigManager,
        credentials: CredentialService,
        drivers: tuple[AuthDriver, ...],
        *,
        clock: Callable[[], datetime] = utc_now,
        operation_limit: int = 64,
        begin_timeout: float = 30.0,
    ) -> None:
        if operation_limit < 1 or not math.isfinite(begin_timeout) or begin_timeout <= 0:
            raise ValueError("authorization limits must be positive")
        self._drivers = {driver.descriptor.driver_id: driver for driver in drivers}
        if len(self._drivers) != len(drivers):
            raise ValueError("authorization driver ids must be unique")
        self._config = config
        self._credentials = credentials
        self._clock = clock
        self._operation_limit = operation_limit
        self._begin_timeout = begin_timeout
        self._operations: dict[str, _Operation] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def methods(self, provider_id: str) -> tuple[AuthorizationMethod, ...]:
        binding = await self._binding(provider_id)
        return () if binding.driver_id == "static" else self._driver(binding).descriptor.methods

    async def begin(self, provider_id: str, method_id: str) -> LoginChallenge:
        self._require_open()
        binding = await self._binding(provider_id)
        driver = self._driver(binding)
        if method_id not in {method.method_id for method in driver.descriptor.methods}:
            raise AuthError("authorization_unsupported")
        assert binding.credential_id is not None  # Validated managed configuration.
        original = await self._credentials.status(binding.credential_id)
        if original is not None and original.driver_id != binding.driver_id:
            raise AuthError("credential_conflict")
        self._require_open()
        self._make_room()
        operation_id = new_id("auth")
        challenge: asyncio.Future[LoginChallenge] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(
            self._login(
                operation_id,
                binding,
                driver,
                method_id,
                original.revision if original is not None else 0,
                challenge,
            )
        )
        self._operations[operation_id] = _Operation(provider_id, task)
        task.add_done_callback(lambda completed: self._completed(completed, challenge))
        try:
            return await asyncio.shield(challenge)
        except asyncio.CancelledError:
            outcome = await self.cancel(operation_id)
            if outcome.state == "succeeded":
                # A fast login may already have crossed save admission before
                # cancellation of the begin waiter reached its owned task.
                return challenge.result()
            raise

    async def _login(
        self,
        operation_id: str,
        binding: ProviderAuthBinding,
        driver: AuthDriver,
        method_id: str,
        revision: int,
        challenge: asyncio.Future[LoginChallenge],
    ) -> CredentialSummary:
        attempt: AuthorizationAttempt | None = None
        try:
            try:
                timer = asyncio.timeout(self._begin_timeout)
                try:
                    async with timer:
                        attempt = await driver.begin_authorization(method_id)
                except TimeoutError:
                    raise AuthError(
                        "authorization_expired" if timer.expired() else "authorization_failed"
                    ) from None
                if timer.expired():
                    raise AuthError("authorization_expired")
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError
                challenge.set_result(
                    LoginChallenge(operation_id=operation_id, challenge=attempt.challenge)
                )
                grant = await attempt.wait()
            finally:
                if attempt is not None:
                    await attempt.close()
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            assert binding.credential_id is not None
            record = await self._credentials.commit(
                binding.credential_id,
                binding.driver_id,
                grant,
                expected_revision=revision,
                binding_guard=binding_guard(self._config, binding),
            )
            return CredentialSummary.from_record(record)
        except AuthError:
            raise
        except Exception:
            raise AuthError("authorization_failed") from None

    @staticmethod
    def _completed(
        task: asyncio.Task[CredentialSummary], challenge: asyncio.Future[LoginChallenge]
    ) -> None:
        error = AuthError("authorization_cancelled") if task.cancelled() else task.exception()
        if not challenge.done():
            challenge.set_exception(error or AuthError("authorization_failed"))
        # A cancelled begin caller may no longer be waiting for this future.
        challenge.exception()

    def operation(self, operation_id: str) -> LoginOperation:
        return self._view(operation_id, self._operation(operation_id))

    @staticmethod
    def _view(operation_id: str, entry: _Operation) -> LoginOperation:
        task = entry.task
        state: OperationState = "pending"
        error: AuthError | None = None
        credential = None
        if task.cancelled():
            state = "cancelled"
            error = AuthError("authorization_cancelled")
        elif task.done():
            failure = task.exception()
            if failure is None:
                state, credential = "succeeded", task.result()
            else:
                error = (
                    failure if isinstance(failure, AuthError) else AuthError("authorization_failed")
                )
                state = "failed"
                if error.code == "authorization_expired":
                    state = "expired"
                elif error.code == "authorization_cancelled":
                    state = "cancelled"
        return LoginOperation(
            operation_id=operation_id,
            provider_id=entry.provider_id,
            state=state,
            error_code=error.code if error else None,
            message=str(error) if error else None,
            credential=credential,
        )

    async def wait(self, operation_id: str) -> LoginOperation:
        entry = self._operation(operation_id)
        task = entry.task
        try:
            await asyncio.shield(task)
        except AuthError:
            pass  # Public failure facts are projected from the terminal task below.
        except asyncio.CancelledError:
            waiter = asyncio.current_task()
            if waiter is not None and waiter.cancelling():
                raise
        return self._view(operation_id, entry)

    async def cancel(self, operation_id: str) -> LoginOperation:
        entry = self._operation(operation_id)
        task = entry.task
        if not task.done() and not task.cancelling():
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError, AuthError:
                continue
        return self._view(operation_id, entry)

    async def status(self, provider_id: str) -> ProviderAuthStatus:
        binding = await self._binding(provider_id)
        if binding.credential_id is None:
            return ProviderAuthStatus(
                provider_id=provider_id,
                driver_id=binding.driver_id,
                credential_id=None,
                state="static",
            )
        bound = await self._bound_providers(binding.credential_id)
        try:
            credential = await self._credentials.status(binding.credential_id)
            driver = self._driver(binding)
            if credential is not None and credential.driver_id != binding.driver_id:
                raise AuthError("credential_conflict")
        except AuthError as error:
            return ProviderAuthStatus(
                provider_id=provider_id,
                driver_id=binding.driver_id,
                credential_id=binding.credential_id,
                state="unavailable",
                error_code=error.code,
                bound_provider_ids=bound,
            )
        state: ProviderAuthState = "missing"
        if credential is not None:
            if credential.state != "active":
                state = credential.state
            elif credential.expires_at is not None and credential.expires_at <= self._clock():
                state = "refresh_needed" if driver.refresh is not None else "reauth_required"
            else:
                state = "ready"
        return ProviderAuthStatus(
            provider_id=provider_id,
            driver_id=binding.driver_id,
            credential_id=binding.credential_id,
            state=state,
            credential=credential,
            bound_provider_ids=bound,
        )

    async def logout(self, provider_id: str) -> CredentialView:
        self._require_open()
        binding = await self._binding(provider_id)
        if binding.credential_id is None:
            raise AuthError("authorization_unsupported")
        credential = await self._credentials.logout(binding.credential_id, binding.driver_id)
        return CredentialView(
            credential=credential,
            bound_provider_ids=await self._bound_providers(binding.credential_id),
        )

    async def credentials(self) -> tuple[CredentialView, ...]:
        summaries = await self._credentials.list_credentials()
        bindings = await asyncio.to_thread(self._bindings)
        return tuple(
            CredentialView(
                credential=summary,
                bound_provider_ids=tuple(
                    binding.provider_id
                    for binding in bindings
                    if binding.credential_id == summary.credential_id
                ),
            )
            for summary in summaries
        )

    async def forget(self, credential_id: str) -> CredentialView:
        self._require_open()
        current = await self._credentials.status(credential_id)
        bindings = await asyncio.to_thread(self._bindings)
        bound = tuple(binding for binding in bindings if binding.credential_id == credential_id)
        driver_id = (
            current.driver_id if current is not None else bound[0].driver_id if bound else None
        )
        if driver_id is None:
            raise AuthError("authentication_required")
        result = await self._credentials.logout(credential_id, driver_id)
        return CredentialView(
            credential=result, bound_provider_ids=tuple(binding.provider_id for binding in bound)
        )

    async def close(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        while True:
            try:
                await asyncio.shield(self._close_task)
                return
            except asyncio.CancelledError:
                if self._close_task.done():
                    self._close_task.result()
                    return

    async def _close(self) -> None:
        pending = tuple(key for key, entry in self._operations.items() if not entry.task.done())
        results = await asyncio.gather(*(self.cancel(key) for key in pending))
        if any(result.error_code == "authorization_cleanup_failed" for result in results):
            raise AuthError("authorization_cleanup_failed")

    def _operation(self, operation_id: str) -> _Operation:
        try:
            return self._operations[operation_id]
        except KeyError:
            raise AuthError("authorization_operation_missing") from None

    def _make_room(self) -> None:
        if len(self._operations) < self._operation_limit:
            return
        for key, entry in self._operations.items():
            if entry.task.done():
                del self._operations[key]
                return
        raise AuthError("authorization_operation_limit")

    def _driver(self, binding: ProviderAuthBinding) -> AuthDriver:
        driver = self._drivers.get(binding.driver_id)
        if driver is None:
            raise AuthError("authorization_unsupported")
        return driver

    def _require_open(self) -> None:
        if self._closed:
            raise AuthError("auth_closed")

    async def _binding(self, provider_id: str) -> ProviderAuthBinding:
        bindings = await asyncio.to_thread(self._bindings)
        for binding in bindings:
            if binding.provider_id == provider_id:
                return binding
        raise AuthError("authorization_configuration_error")

    def _bindings(self) -> tuple[ProviderAuthBinding, ...]:
        try:
            return tuple(
                ProviderAuthBinding.from_provider(provider)
                for provider in self._config.snapshot().providers
            )
        except ConfigError:
            raise AuthError("authorization_configuration_error") from None

    async def _bound_providers(self, credential_id: str) -> tuple[str, ...]:
        bindings = await asyncio.to_thread(self._bindings)
        return tuple(
            binding.provider_id for binding in bindings if binding.credential_id == credential_id
        )
