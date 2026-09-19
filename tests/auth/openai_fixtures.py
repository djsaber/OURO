from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

NOW = datetime(2026, 9, 18, tzinfo=UTC)


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


def tokens(**overrides):
    return {
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "id_token": jwt(
            **{"https://api.openai.com/auth": {"chatgpt_account_id": "account-secret"}}
        ),
        "expires_in": 3600,
        **overrides,
    }
