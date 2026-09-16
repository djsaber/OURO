from __future__ import annotations

import pytest

from ouro import local_application
from ouro.application.provider_auth import ProviderAuthService
from ouro.auth.contracts import AuthError
from ouro.auth.credentials import CredentialService
from ouro.core.errors import ConfigError


@pytest.mark.parametrize(
    "body_failure,cleanup_failure", [(False, False), (True, False), (False, True)]
)
async def test_lightweight_composition_always_closes_credentials_after_logins(
    tmp_path, monkeypatch, body_failure, cleanup_failure
):
    events = []
    close_auth = ProviderAuthService.close
    close_credentials = CredentialService.close

    async def auth_close(self):
        await close_auth(self)
        events.append("auth")
        if cleanup_failure:
            raise AuthError("authorization_cleanup_failed")

    async def credentials_close(self):
        await close_credentials(self)
        events.append("credentials")

    def must_not_build(*args, **kwargs):
        pytest.fail("authentication management must not build a model runtime")

    monkeypatch.setattr(ProviderAuthService, "close", auth_close)
    monkeypatch.setattr(CredentialService, "close", credentials_close)
    monkeypatch.setattr(local_application, "build_local_runtime", must_not_build)
    monkeypatch.setattr(local_application, "build_local_session_services", must_not_build)
    monkeypatch.setattr(local_application, "_build_local_plugin_runtime", must_not_build)

    async def use():
        async with local_application.open_local_provider_auth(tmp_path) as service:
            assert await service.methods("faux") == ()
            if body_failure:
                raise ConfigError("fixture body failure")
        with pytest.raises(AuthError, match="closed"):
            await service.begin("faux", "unsupported")

    if cleanup_failure:
        with pytest.raises(AuthError) as error:
            await use()
        assert error.value.code == "authorization_cleanup_failed"
    elif body_failure:
        with pytest.raises(ConfigError, match="fixture body"):
            await use()
    else:
        await use()
    assert events == ["auth", "credentials"]
    assert list(tmp_path.iterdir()) == []
