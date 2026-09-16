from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from ouro.auth.authorization import TaskAuthorizationAttempt
from ouro.auth.contracts import AuthError, CredentialGrant
from ouro.auth.driver_contracts import (
    AuthDriverDescriptor,
    AuthorizationChallenge,
    AuthorizationMethod,
    BrowserAuthorizationChallenge,
    DeviceAuthorizationChallenge,
    RequestAuthMaterial,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def challenge(seconds: float = 30) -> BrowserAuthorizationChallenge:
    return BrowserAuthorizationChallenge(
        authorization_url="https://fixture.invalid/authorize?state=fixture-secret",
        expires_at=NOW + timedelta(seconds=seconds),
    )


def grant() -> CredentialGrant:
    return CredentialGrant(secret_payload={"token": "fixture-token"})


def test_closed_challenges_round_trip_without_secret_repr() -> None:
    adapter = TypeAdapter(AuthorizationChallenge)
    values = (
        challenge(),
        DeviceAuthorizationChallenge(
            verification_uri="https://fixture.invalid/activate",
            verification_uri_complete="https://fixture.invalid/activate?code=fixture-code",
            user_code="fixture-code",
            expires_at=NOW,
        ),
    )
    for value in values:
        assert adapter.validate_json(value.model_dump_json()) == value
        assert "fixture" not in repr(value)
    for update in (
        {"kind": "custom"},
        {"verifier": "fixture-secret"},
        {"authorization_url": "javascript:fixture-secret"},
        {"expires_at": "2026-09-16T00:00:00"},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python({**challenge().model_dump(), **update})


def test_methods_have_stable_unique_ids_and_static_is_not_a_driver() -> None:
    method = AuthorizationMethod(method_id="browser", kind="browser_callback", label="Browser")
    descriptor = AuthDriverDescriptor(driver_id="fixture", label="Fixture", methods=(method,))
    assert descriptor.methods[0].method_id == "browser"
    for update in ({"methods": (method, method)}, {"driver_id": "static"}, {"methods": ()}):
        with pytest.raises(ValidationError):
            AuthDriverDescriptor.model_validate({**descriptor.model_dump(), **update})
    with pytest.raises(ValidationError):
        AuthorizationMethod(method_id="1", kind="device_code", label="Device")


def test_driver_material_headers_are_a_private_immutable_snapshot() -> None:
    headers = {"Authorization": "Bearer fixture-secret"}
    material = RequestAuthMaterial(headers=headers, account_scope=("fixture-account",))
    headers["Authorization"] = "changed"
    assert material.headers["Authorization"] == "Bearer fixture-secret"
    assert "fixture-secret" not in repr(material)
    with pytest.raises(TypeError):
        material.headers["Authorization"] = "changed"  # type: ignore[index]


async def test_result_is_repeatable_and_success_waits_for_cleanup() -> None:
    cleaning, finish = asyncio.Event(), asyncio.Event()
    exchanges = cleanups = 0

    async def authorize() -> CredentialGrant:
        nonlocal exchanges
        exchanges += 1
        return grant()

    async def cleanup() -> None:
        nonlocal cleanups
        cleanups += 1
        cleaning.set()
        await finish.wait()

    attempt = TaskAuthorizationAttempt(
        challenge(), authorize=authorize, cleanup=cleanup, clock=lambda: NOW
    )
    await cleaning.wait()
    waiting = asyncio.create_task(attempt.wait())
    await asyncio.sleep(0)
    assert not waiting.done()
    finish.set()
    assert await waiting == grant()
    assert await attempt.wait() == grant()
    await attempt.close()
    assert exchanges == cleanups == 1


async def test_cancelled_waiter_does_not_cancel_the_owned_exchange() -> None:
    started, finish = asyncio.Event(), asyncio.Event()
    cleaned = False

    async def authorize() -> CredentialGrant:
        started.set()
        await finish.wait()
        return grant()

    async def cleanup() -> None:
        nonlocal cleaned
        cleaned = True

    attempt = TaskAuthorizationAttempt(
        challenge(), authorize=authorize, cleanup=cleanup, clock=lambda: NOW
    )
    waiting = asyncio.create_task(attempt.wait())
    await started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not cleaned
    finish.set()
    assert await attempt.wait() == grant()
    assert cleaned


async def test_cancel_before_exchange_starts_still_releases_prepared_resources() -> None:
    cleanups = 0

    async def authorize() -> CredentialGrant:
        pytest.fail("an immediately cancelled authorization must not start I/O")

    async def cleanup() -> None:
        nonlocal cleanups
        cleanups += 1

    attempt = TaskAuthorizationAttempt(
        challenge(), authorize=authorize, cleanup=cleanup, clock=lambda: NOW
    )
    await attempt.cancel()
    await attempt.close()
    assert cleanups == 1
    with pytest.raises(AuthError) as failure:
        await attempt.wait()
    assert failure.value.code == "authorization_cancelled"


async def test_concurrent_close_and_caller_cancellation_finish_driver_cleanup_once() -> None:
    started, finalizing, release, cleaned = (asyncio.Event() for _ in range(4))

    async def authorize() -> CredentialGrant:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizing.set()
            await release.wait()
        raise AssertionError("unreachable")

    async def cleanup() -> None:
        cleaned.set()

    attempt = TaskAuthorizationAttempt(
        challenge(), authorize=authorize, cleanup=cleanup, clock=lambda: NOW
    )
    await started.wait()
    first = asyncio.create_task(attempt.close())
    await finalizing.wait()
    second = asyncio.create_task(attempt.cancel())
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    assert not cleaned.is_set()
    release.set()
    await asyncio.gather(first, second)
    assert cleaned.is_set()


@pytest.mark.parametrize("seconds", [0, 0.02])
async def test_deadline_expires_and_cleans_up_even_without_a_waiter(seconds: float) -> None:
    cleaned = asyncio.Event()

    async def authorize() -> CredentialGrant:
        assert seconds > 0
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def cleanup() -> None:
        cleaned.set()

    attempt = TaskAuthorizationAttempt(
        challenge(seconds), authorize=authorize, cleanup=cleanup, clock=lambda: NOW
    )
    await asyncio.wait_for(cleaned.wait(), 1)
    with pytest.raises(AuthError) as failure:
        await attempt.wait()
    assert failure.value.code == "authorization_expired"
    await attempt.close()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AuthError("authorization_denied"), "authorization_denied"),
        (OSError("callback?code=fixture-secret"), "authorization_failed"),
        (TimeoutError("response=fixture-secret"), "authorization_failed"),
    ],
)
async def test_exchange_errors_are_safe_and_network_timeout_is_not_challenge_expiry(
    error, code
) -> None:
    cleanups = 0

    async def authorize() -> CredentialGrant:
        raise error

    async def cleanup() -> None:
        nonlocal cleanups
        cleanups += 1

    attempt = TaskAuthorizationAttempt(
        challenge(), authorize=authorize, cleanup=cleanup, clock=lambda: NOW
    )
    with pytest.raises(AuthError) as failure:
        await attempt.wait()
    assert failure.value.code == code
    assert "fixture-secret" not in str(failure.value)
    assert cleanups == 1
    await attempt.close()


@pytest.mark.parametrize("mode", ["raise", "hang", "cancel", "ignore-timeout"])
async def test_cleanup_failure_is_visible_and_cannot_report_a_successful_grant(mode: str) -> None:
    cleanups = 0

    async def authorize() -> CredentialGrant:
        return grant()

    async def cleanup() -> None:
        nonlocal cleanups
        cleanups += 1
        if mode == "raise":
            raise OSError("fixture-secret")
        if mode == "cancel":
            raise asyncio.CancelledError
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if mode != "ignore-timeout":
                raise

    attempt = TaskAuthorizationAttempt(
        challenge(), authorize=authorize, cleanup=cleanup, clock=lambda: NOW, cleanup_timeout=0.02
    )
    for action in (attempt.wait, attempt.close):
        with pytest.raises(AuthError) as failure:
            await action()
        assert failure.value.code == "authorization_cleanup_failed"
        assert "fixture-secret" not in str(failure.value)
    assert cleanups == 1


async def test_close_during_cleanup_preserves_material_already_received() -> None:
    cleaning, finish = asyncio.Event(), asyncio.Event()

    async def authorize() -> CredentialGrant:
        return grant()

    async def cleanup() -> None:
        cleaning.set()
        await finish.wait()

    attempt = TaskAuthorizationAttempt(
        challenge(), authorize=authorize, cleanup=cleanup, clock=lambda: NOW
    )
    await cleaning.wait()
    closing = asyncio.create_task(attempt.close())
    await asyncio.sleep(0)
    assert not closing.done()
    finish.set()
    await closing
    assert await attempt.wait() == grant()


@pytest.mark.parametrize("expire", [False, True])
async def test_late_grant_after_driver_suppresses_cancellation_is_not_accepted(
    expire: bool,
) -> None:
    started = asyncio.Event()
    returned = False

    async def authorize() -> CredentialGrant:
        nonlocal returned
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            returned = True
            return grant()

    async def cleanup() -> None:
        pass

    attempt = TaskAuthorizationAttempt(
        challenge(1 if expire else 30), authorize=authorize, cleanup=cleanup, clock=lambda: NOW
    )
    if not expire:
        await asyncio.wait_for(started.wait(), 5)
        await attempt.cancel()
    with pytest.raises(AuthError) as failure:
        await attempt.wait()
    assert failure.value.code == ("authorization_expired" if expire else "authorization_cancelled")
    assert returned
    await attempt.close()


@pytest.mark.parametrize("outcome", ["success", "denied", "expired", "cancelled"])
async def test_attempt_releases_an_actual_listener_on_every_terminal_path(outcome: str) -> None:
    async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def authorize() -> CredentialGrant:
        if outcome == "success":
            return grant()
        if outcome == "denied":
            raise AuthError("authorization_denied")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def cleanup() -> None:
        server.close()
        await server.wait_closed()

    attempt = TaskAuthorizationAttempt(
        challenge(0.02 if outcome == "expired" else 30),
        authorize=authorize,
        cleanup=cleanup,
        clock=lambda: NOW,
    )
    try:
        if outcome == "cancelled":
            await attempt.cancel()
        if outcome == "success":
            assert await attempt.wait() == grant()
        else:
            with pytest.raises(AuthError) as failure:
                await attempt.wait()
            assert failure.value.code == f"authorization_{outcome}"
        assert not server.is_serving()
        replacement = await asyncio.start_server(connected, "127.0.0.1", port)
        replacement.close()
        await replacement.wait_closed()
    finally:
        with suppress(AuthError):
            await attempt.close()
        server.close()
        await server.wait_closed()
