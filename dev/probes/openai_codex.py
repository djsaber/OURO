"""Run explicitly: python -m dev.probes.openai_codex --help."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import webbrowser
from typing import Any

import httpx

from dev.probes.codex_adapter import CODEX_REQUEST_POLICY, verify_adapter
from dev.probes.codex_oauth import (
    CODEX_REVISION,
    OPENCODE_REVISION,
    CodexOAuthProbe,
    ProbeAuthorizationError,
    ProbeError,
)
from dev.probes.codex_responses import verify_responses
from ouro.auth.contracts import AuthError, CredentialGrant
from ouro.auth.driver_contracts import BrowserAuthorizationChallenge
from ouro.auth.drivers.openai_errors import OpenAIOAuthError
from ouro.core.provider import ModelMetadata


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Memory-only Codex compatibility experiment; no production support claim. "
            "Login, refresh once, and verify four small model requests."
        )
    )
    result.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="explicitly enable real login and subscription requests",
    )
    result.add_argument("--method", choices=("browser", "device"), required=True)
    result.add_argument("--model", required=True, help="exact model id to verify; no default claim")
    result.add_argument("--open-browser", action="store_true", help="open the displayed login URL")
    result.add_argument(
        "--adapter",
        action="store_true",
        help="verify the production Responses adapter, including replay across refresh",
    )
    result.add_argument(
        "--max-output-tokens",
        type=int,
        help="local output budget for --adapter; no Codex request output limit (default: 8192)",
    )
    return result


async def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    report: dict[str, Any] = {
        "schema_version": 1,
        "experiment": "openai-codex",
        "method": args.method,
        "model": args.model,
        "codex_revision": CODEX_REVISION,
        "opencode_revision": OPENCODE_REVISION,
        "production_support": False,
        "completed_steps": [],
        "status": "failed",
    }
    stage = "authorization"
    try:
        adapter_mode = getattr(args, "adapter", False)
        limit = getattr(args, "max_output_tokens", None)
        if limit is not None and not adapter_mode:
            raise ProbeError("adapter_required_for_output_limit")
        if adapter_mode:
            try:
                metadata = ModelMetadata(
                    model_id=args.model,
                    provider_id="openai-responses",
                    max_output_tokens=8192 if limit is None else limit,
                )
            except ValueError:
                raise ProbeError("invalid_output_limit") from None
            report.update(
                request_path="production_adapter",
                max_output_tokens=metadata.max_output_tokens,
                output_limit_enforced=CODEX_REQUEST_POLICY.supports_output_limit,
            )
            print(
                "This Codex integration has no request output limit; max_output_tokens is a local "
                "planning budget only and is not sent to this service.",
                file=sys.stderr,
                flush=True,
            )
        async with httpx.AsyncClient(follow_redirects=False) as client:
            oauth = CodexOAuthProbe(client)
            attempt = await (oauth.browser() if args.method == "browser" else oauth.device())
            try:
                challenge = attempt.challenge
                if isinstance(challenge, BrowserAuthorizationChallenge):
                    url = str(challenge.authorization_url)
                    print("Open this login URL:\n" + url, file=sys.stderr, flush=True)
                else:
                    url = str(challenge.verification_uri)
                    print(
                        f"Open {url}\nDevice code: {challenge.user_code}",
                        file=sys.stderr,
                        flush=True,
                    )
                print(
                    "[1/3] Waiting for authorization and token exchange...",
                    file=sys.stderr,
                    flush=True,
                )
                if args.open_browser:
                    await asyncio.to_thread(webbrowser.open, url)
                grant = await attempt.wait()
            finally:
                await attempt.close()
            report["completed_steps"].append("authorization")
            report["expiry_provided"] = grant.expires_at is not None
            observations: list[dict[str, Any]] = []
            report["responses"] = observations

            async def refresh(current: CredentialGrant) -> CredentialGrant:
                nonlocal stage
                stage = "refresh"
                print(
                    "[2/3] Verifying token refresh..."
                    if adapter_mode
                    else "[2/3] Authorization complete; verifying token refresh...",
                    file=sys.stderr,
                    flush=True,
                )
                refreshed = await oauth.refresh(current)
                report["refresh_token_rotated"] = (
                    refreshed.secret_payload["refresh_token"]
                    != current.secret_payload["refresh_token"]
                )
                report["completed_steps"].append("refresh")
                stage = "responses"
                print(
                    "[3/3] Refresh complete; verifying adapter replay and two tool rounds..."
                    if adapter_mode
                    else "[3/3] Refresh complete; verifying four model requests "
                    "(text, two tool rounds, continuation)...",
                    file=sys.stderr,
                    flush=True,
                )
                return refreshed

            if adapter_mode:
                stage = "responses"
                print(
                    "Authorization complete; verifying adapter text before refresh...",
                    file=sys.stderr,
                    flush=True,
                )
                await verify_adapter(
                    client,
                    grant,
                    model=args.model,
                    max_output_tokens=metadata.max_output_tokens,
                    refresh=refresh,
                    observations=observations,
                )
            else:
                refreshed = await refresh(grant)
                await verify_responses(
                    client, refreshed, model=args.model, observations=observations
                )
            report["completed_steps"].append("responses")
            report["status"] = "passed"
            print("Verification passed. Preparing JSON summary.", file=sys.stderr, flush=True)
            return report, 0
    except ProbeError as error:
        report.update(error_code=error.code, http_status=error.status)
    except ProbeAuthorizationError as error:
        report.update(error_code=error.probe_code, http_status=error.http_status)
    except OpenAIOAuthError as error:
        report.update(error_code=error.reason, http_status=error.status)
    except AuthError as error:
        report["error_code"] = error.code
    except asyncio.CancelledError:
        report.update(status="cancelled", error_code="cancelled")
        report["stage"] = stage
        return report, 130
    except Exception:
        # External exceptions and tracebacks may contain tokens or callback URLs.
        report["error_code"] = "probe_internal_error"
    report["stage"] = stage
    return report, 1


def main() -> int:
    arguments = parser().parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", arguments.model):
        parser().error("model must be a model identifier")
    report, exit_code = asyncio.run(run(arguments))
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
