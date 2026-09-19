from __future__ import annotations

from typing import Literal

from ouro.auth.contracts import AuthError, AuthErrorCode

OAuthFailure = Literal[
    "invalid_response",
    "missing_refresh_token",
    "ambiguous_account_route",
    "unsupported_residency",
    "invalid_token_expiry",
    "callback_port_unavailable",
    "auth_network_error",
    "auth_http_error",
]


class OpenAIOAuthError(AuthError):
    """Fixed local facts only; never retain an external response or callback URL."""

    def __init__(
        self,
        reason: OAuthFailure,
        *,
        code: AuthErrorCode = "authorization_failed",
        status: int | None = None,
    ) -> None:
        super().__init__(code)
        self.reason = reason
        self.status = status
