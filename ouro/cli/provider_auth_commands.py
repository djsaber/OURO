from __future__ import annotations

import argparse
import asyncio
import json
import sys
import webbrowser

from ouro.application.contracts.provider_auth import CredentialView, LoginChallenge, LoginOperation
from ouro.application.provider_auth.service import AuthError, ProviderAuthService
from ouro.cli.errors import error_exit_code
from ouro.core.errors import ConfigError, OuroError
from ouro.local_application import open_local_provider_auth


async def run_provider_auth_command(args: argparse.Namespace) -> int:
    """Present Application results without owning login or credential state."""
    try:
        async with open_local_provider_auth(args.home) as service:
            payload, text, code = await _command(service, args)
    except asyncio.CancelledError:
        return _print_error(AuthError("authorization_cancelled"), json_mode=args.json)
    except OuroError as error:
        return _print_error(error, json_mode=args.json)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(text, file=sys.stdout if code == 0 else sys.stderr)
    return code


async def _command(
    service: ProviderAuthService, args: argparse.Namespace
) -> tuple[dict[str, object], str, int]:
    command = args.auth_command
    if command == "methods":
        methods = await service.methods(args.provider_id)
        return (
            {
                "status": "ok",
                "provider_id": args.provider_id,
                "methods": [method.model_dump(mode="json") for method in methods],
            },
            "\n".join(f"{method.method_id}: {method.label} ({method.kind})" for method in methods)
            or "No interactive login methods available.",
            0,
        )
    if command == "login":
        result = await _login(service, args)
        code = 0 if result.state == "succeeded" else 5 if result.state == "cancelled" else 3
        return (
            {
                "status": "ok" if code == 0 else "failed",
                "operation": result.model_dump(mode="json"),
            },
            f"Login {result.state}: {result.provider_id}"
            + (f"\n{result.message}" if result.message else ""),
            code,
        )
    if command == "status":
        status = await service.status(args.provider_id)
        code = 3 if status.state == "unavailable" else 0
        return (
            {
                "status": "ok" if code == 0 else "failed",
                "authentication": status.model_dump(mode="json"),
            },
            f"Provider: {status.provider_id}\nAuthentication: {status.state}\n"
            f"Credential: {status.credential_id or 'static'}\n"
            f"References: {', '.join(status.bound_provider_ids) or '-'}"
            + (f"\nError: {status.error_code}" if status.error_code else ""),
            code,
        )
    if command == "list":
        credentials = await service.credentials()
        return (
            {"status": "ok", "credentials": [view.model_dump(mode="json") for view in credentials]},
            "\n".join(_credential_text(view) for view in credentials) or "No local credentials.",
            0,
        )
    if command == "logout":
        cleared = await service.logout(args.provider_id)
    elif command == "forget":
        cleared = await service.forget(args.credential_id)
    else:
        raise AssertionError(f"unknown authentication command: {command}")
    return (
        {"status": "ok", **cleared.model_dump(mode="json")},
        _credential_text(cleared),
        0,
    )


async def _login(service: ProviderAuthService, args: argparse.Namespace) -> LoginOperation:
    method_id = args.method
    if method_id is None:
        methods = await service.methods(args.provider_id)
        if not methods:
            raise AuthError("authorization_unsupported")
        if len(methods) != 1:
            raise ConfigError("choose --method from 'provider auth methods PROVIDER'")
        method_id = methods[0].method_id
    started = await service.begin(args.provider_id, method_id)
    try:
        url = _show_challenge(started)
        if args.open_browser:
            try:
                opened = await asyncio.to_thread(webbrowser.open, url, new=2)
            except OSError, webbrowser.Error:
                opened = False
            if not opened:
                print("Could not open a browser; use the authorization URL above.", file=sys.stderr)
        return await service.wait(started.operation_id)
    except asyncio.CancelledError:
        # Commit may have won cancellation. Only Application can determine the
        # durable result; never turn a successful save into a cancelled report.
        return await service.cancel(started.operation_id)


def _show_challenge(started: LoginChallenge) -> str:
    challenge = started.challenge
    if challenge.kind == "browser_callback":
        url = str(challenge.authorization_url)
        print(f"Authorize in your browser: {url}", file=sys.stderr, flush=True)
    else:
        url = str(challenge.verification_uri_complete or challenge.verification_uri)
        print(f"Open: {url}\nDevice code: {challenge.user_code}", file=sys.stderr, flush=True)
    print(f"Expires: {challenge.expires_at.isoformat()}", file=sys.stderr, flush=True)
    if challenge.instructions:
        print(challenge.instructions, file=sys.stderr, flush=True)
    return url


def _credential_text(view: CredentialView) -> str:
    credential = view.credential
    return (
        f"{credential.credential_id}: {credential.state} ({credential.driver_id})\n"
        f"  References: {', '.join(view.bound_provider_ids) or '-'}"
    )


def _print_error(error: OuroError, *, json_mode: bool) -> int:
    code = (
        5
        if isinstance(error, AuthError) and error.code == "authorization_cancelled"
        else error_exit_code(error)
    )
    payload: dict[str, object] = {
        "status": "failed",
        "error": str(error),
        "error_type": type(error).__name__,
    }
    if isinstance(error, AuthError):
        payload["error_code"] = error.code
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Error: {error}", file=sys.stderr)
    return code
