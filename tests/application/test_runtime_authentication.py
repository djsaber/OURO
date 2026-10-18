from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ouro.application.summary_compaction import LLMSummaryCompactionStrategy
from ouro.auth.contracts import AuthError, CredentialGrant
from ouro.auth.credentials import CredentialService
from ouro.auth.managed_requests import RequestTarget
from ouro.auth.store import FileCredentialStore
from ouro.config import provider_models
from ouro.config.compaction import CompactionConfigManager
from ouro.config.provider_models import ProviderConnectionSpec, ProviderTypeSpec
from ouro.config.providers import ProviderConfigManager
from ouro.core.errors import ConfigError
from ouro.core.state import SessionRecord
from ouro.local_application import LocalApplication, ProviderServicePolicy, build_local_runtime
from ouro.providers.authentication import ProviderRequestPolicy
from ouro.providers.http_transport import ProviderTransportError
from tests.application.test_provider_auth import ControlledDriver
from tests.auth import test_driver_contracts as fixtures


@pytest.fixture
def managed_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(fixtures, "NOW", datetime.now(UTC))
    drivers = (fixtures.BrowserFixtureDriver(), fixtures.DeviceFixtureDriver())
    monkeypatch.setitem(
        provider_models._PROVIDER_TYPE_SPECS,
        "custom",
        ProviderTypeSpec(
            "custom",
            ProviderConnectionSpec("openai-chat-completions"),
            tuple(
                ProviderConnectionSpec("openai-chat-completions", d.descriptor.driver_id, "fixture")
                for d in drivers
            ),
        ),
    )
    policies = (
        ProviderServicePolicy("fixture", (RequestTarget("https://fixture.invalid", "/api"),)),
    )
    configs = ProviderConfigManager(tmp_path)
    provider = configs.create_provider(
        display_name="Subscription",
        provider_type="custom",
        protocol="openai-chat-completions",
        auth_driver="fixture-browser",
        base_url="https://fixture.invalid/api",
    )
    endpoint_ids = {}
    for endpoint in ("chat", "summary", "child"):
        configured = configs.create_endpoint(
            provider.provider_id, model_id=endpoint, supports_streaming=False
        )
        endpoint_ids[endpoint] = configured.endpoint_id
    configs.set_default_route(provider.provider_id, endpoint_ids["chat"])
    CompactionConfigManager(tmp_path).update_model(
        provider_id=provider.provider_id, endpoint_id=endpoint_ids["summary"]
    )
    requests = []

    class Transport:
        async def post_json(self, **request):
            requests.append(request)
            return {
                "choices": [
                    {"message": {"content": "<summary>ok</summary>"}, "finish_reason": "stop"}
                ]
            }

        async def aclose(self):
            pass

    monkeypatch.setattr(
        "ouro.providers.openai_chat_completions.HttpxProviderHTTPTransport", Transport
    )
    return configs, drivers, policies, requests


async def login(auth):
    provider_id = _provider_id(auth._config)
    method = (await auth.methods(provider_id))[0]
    started = await auth.begin(provider_id, method.method_id)
    assert (await auth.wait(started.operation_id)).state == "succeeded"


def _provider_id(configs: ProviderConfigManager) -> str:
    providers = configs.snapshot(include_builtin=False).providers
    assert len(providers) == 1
    return providers[0].provider_id


def _endpoint_id(configs: ProviderConfigManager, model_id: str) -> str:
    provider = configs.get_provider(_provider_id(configs))
    assert provider is not None
    return next(
        endpoint.endpoint_id for endpoint in provider.endpoints if endpoint.model_id == model_id
    )


def _model_key(configs: ProviderConfigManager, model_id: str) -> str:
    return f"{_provider_id(configs)}/{_endpoint_id(configs, model_id)}"


async def test_unlogged_default_starts_and_shared_login_serves_all_model_consumers(
    tmp_path, managed_runtime
):
    configs, drivers, policies, requests = managed_runtime
    app = LocalApplication.build(tmp_path, auth_drivers=drivers, service_policies=policies)
    auth = app.provider_auth
    assert auth is app.runtime.provider_auth
    assert not (tmp_path / "credentials").exists()
    async with app:
        provider_id = _provider_id(configs)
        assert (await auth.status(provider_id)).state == "missing"
        check = await app.runtime.adapter.provider.check_connection(app.runtime.adapter.model)
        assert not check.ok and check.error_code == "authentication_required"
        assert requests == []
        await login(auth)
        result = await app.runtime.run("hello")
        assert result.run.status == "completed"
        strategy = app.runtime.compaction_strategies.get(LLMSummaryCompactionStrategy.strategy_id)
        summary = strategy._adapter_routes(result.session.session_id)[0].adapter
        assert (await summary.provider.check_connection(summary.model)).ok
        child = SessionRecord(namespace="agent", current_model=_model_key(configs, "child"))
        app.runtime.store.create_session(child)
        _, child_adapter = app.runtime._resolve_session_adapter(child)
        assert (await child_adapter.provider.check_connection(child_adapter.model)).ok
        assert {request["payload"]["model"] for request in requests} == {"chat", "summary", "child"}
        assert {request["headers"]["authorization"] for request in requests} == {
            "Bearer fixture-access"
        }
        main = app.runtime.models.adapter_for(_model_key(configs, "chat"))
        await auth.logout(provider_id)
        for adapter in (main, summary, child_adapter):
            assert (
                await adapter.provider.check_connection(adapter.model)
            ).error_code == "authentication_required"
        assert len(requests) == 3
    with pytest.raises(AuthError, match="closed"):
        await auth.begin(_provider_id(configs), "external-browser")


async def test_cached_adapters_observe_new_material_and_binding_changes(tmp_path, managed_runtime):
    configs, drivers, policies, requests = managed_runtime
    runtime = build_local_runtime(tmp_path, auth_drivers=drivers, service_policies=policies)
    try:
        await login(runtime.provider_auth)
        provider_id = _provider_id(configs)
        main = runtime.models.adapter_for(_model_key(configs, "chat"))
        provider = configs.get_provider(provider_id)
        assert provider is not None
        ref = provider.credential_ref.name
        external = CredentialService(FileCredentialStore(tmp_path))
        try:
            await external.commit(
                ref,
                "fixture-browser",
                CredentialGrant(
                    secret_payload={
                        "access": "updated",
                        "refresh": "updated-refresh",
                        "subject": "account-a",
                    }
                ),
                expected_revision=1,
            )
        finally:
            await external.close()
        assert runtime.models.adapter_for(_model_key(configs, "chat")) is main
        assert (await main.provider.check_connection(main.model)).ok
        assert requests[-1]["headers"]["authorization"] == "Bearer updated"
        # Replace keeps the provider identity but regenerates its endpoint identities.
        configs.set_default_provider("faux")
        configs.replace_provider(
            provider_id,
            display_name="Subscription",
            provider_type="custom",
            protocol="openai-chat-completions",
            auth_driver="fixture-device",
            base_url="https://fixture.invalid/api",
        )
        endpoint_ids = {}
        for endpoint in ("chat", "summary"):
            configured = configs.create_endpoint(
                provider_id, model_id=endpoint, supports_streaming=False
            )
            endpoint_ids[endpoint] = configured.endpoint_id
        configs.set_default_route(provider_id, endpoint_ids["chat"])
        new_main = runtime.models.adapter_for(_model_key(configs, "chat"))
        assert new_main is not main
        assert (
            await new_main.provider.check_connection(new_main.model)
        ).error_code == "authentication_required"
        await login(runtime.provider_auth)
        assert (await new_main.provider.check_connection(new_main.model)).ok
        assert "authorization" not in requests[-1]["headers"]
        assert requests[-1]["headers"]["x-fixture-session"] == "fixture-ticket"
        summary = runtime.models.adapter_for(_model_key(configs, "summary"))
        assert (await summary.provider.check_connection(summary.model)).ok
        assert requests[-1]["headers"]["x-fixture-session"] == "fixture-ticket"
    finally:
        await runtime.close()


def test_composition_rejects_missing_driver_or_service_references(tmp_path, managed_runtime):
    _, drivers, policies, _ = managed_runtime
    with pytest.raises(ConfigError, match="driver is not registered"):
        build_local_runtime(tmp_path, service_policies=policies)
    with pytest.raises(ConfigError, match="service policy is not registered"):
        build_local_runtime(tmp_path, auth_drivers=drivers)


@pytest.mark.parametrize("wrapped", [False, True])
async def test_close_finishes_credentials_after_adapter_failure_and_caller_cancellation(
    tmp_path, managed_runtime, monkeypatch, wrapped
):
    _, drivers, policies, _ = managed_runtime
    runtime = build_local_runtime(tmp_path, auth_drivers=drivers, service_policies=policies)
    owner = LocalApplication.from_runtime(runtime) if wrapped else runtime
    credentials = runtime._credential_service
    events = []
    entered, release = asyncio.Event(), asyncio.Event()
    close_credentials = credentials.close
    close_auth = runtime.provider_auth.close

    async def auth_close():
        await close_auth()
        if "auth" not in events:
            events.append("auth")

    async def credentials_close():
        events.append("credentials")
        await close_credentials()

    async def adapter_close():
        events.append("adapter")
        entered.set()
        await release.wait()
        raise RuntimeError("fixture close failure")

    monkeypatch.setattr(runtime.provider_auth, "close", auth_close)
    monkeypatch.setattr(credentials, "close", credentials_close)
    monkeypatch.setattr(runtime.adapter.provider, "close", adapter_close)
    task = asyncio.create_task(owner.close())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and events == ["auth", "adapter"]
    release.set()
    with pytest.raises(ExceptionGroup):
        await task
    assert events == ["auth", "adapter", "credentials"]
    with pytest.raises(AuthError, match="closed"):
        await credentials.list_credentials()


@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_pending_login_is_settled_before_model_consumers_and_credentials(
    tmp_path, managed_runtime, monkeypatch, cleanup_failure
):
    _, drivers, policies, _ = managed_runtime
    driver = ControlledDriver()
    driver.cleanup_error = cleanup_failure
    app = LocalApplication.build(
        tmp_path, auth_drivers=(driver, drivers[1]), service_policies=policies
    )
    started = await app.provider_auth.begin(
        _provider_id(app.provider_auth._config), "external-browser"
    )
    await driver.started.wait()
    close_models = app.runtime.models.close

    async def models_close():
        assert driver.cleaned == 1
        assert app.provider_auth.operation(started.operation_id).state in {"cancelled", "failed"}
        await close_models()

    monkeypatch.setattr(app.runtime.models, "close", models_close)
    if cleanup_failure:
        with pytest.raises(ExceptionGroup):
            await app.close()
    else:
        await app.close()
    assert driver.cleaned == 1
    with pytest.raises(AuthError, match="closed"):
        await app.runtime._credential_service.list_credentials()


async def test_separate_homes_do_not_share_credentials_or_shutdown(tmp_path, managed_runtime):
    configs, drivers, policies, requests = managed_runtime
    original = build_local_runtime(tmp_path, auth_drivers=drivers, service_policies=policies)
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    other_config = ProviderConfigManager(other_home)
    other_config.config_path.parent.mkdir(parents=True, exist_ok=True)
    other_config.config_path.write_bytes(configs.config_path.read_bytes())
    other = build_local_runtime(other_home, auth_drivers=drivers, service_policies=policies)
    try:
        await login(original.provider_auth)
        assert (await other.provider_auth.status(_provider_id(other_config))).state == "missing"
        assert (
            await other.adapter.provider.check_connection(other.adapter.model)
        ).error_code == "authentication_required"
        await other.close()
        assert (await original.adapter.provider.check_connection(original.adapter.model)).ok
        assert len(requests) == 1
    finally:
        await other.close()
        await original.close()


async def test_start_failure_uses_normal_authentication_cleanup(
    tmp_path, managed_runtime, monkeypatch
):
    _, drivers, policies, _ = managed_runtime
    driver = ControlledDriver()
    app = LocalApplication.build(
        tmp_path, auth_drivers=(driver, drivers[1]), service_policies=policies
    )
    await app.provider_auth.begin(_provider_id(app.provider_auth._config), "external-browser")
    await driver.started.wait()

    async def fail_start():
        raise RuntimeError("fixture startup failure")

    monkeypatch.setattr(app.mcp, "start", fail_start)
    with pytest.raises(RuntimeError, match="startup failure"):
        await app.start()
    assert driver.cleaned == 1
    with pytest.raises(AuthError, match="closed"):
        await app.runtime._credential_service.list_credentials()
    await app.close()


async def test_static_product_service_policy_is_applied_without_managed_target_restrictions(
    tmp_path, managed_runtime, monkeypatch
):
    configs, drivers, _, requests = managed_runtime
    old = provider_models.provider_type_spec("custom")
    monkeypatch.setitem(
        provider_models._PROVIDER_TYPE_SPECS,
        "custom",
        ProviderTypeSpec(
            "custom",
            ProviderConnectionSpec("openai-chat-completions", "static", "fixture"),
            old.alternative_connections,
        ),
    )
    policy = ProviderServicePolicy(
        "fixture",
        (RequestTarget("https://fixture.invalid", "/api"),),
        ProviderRequestPolicy(frozenset({"fixture-quota"})),
    )
    configs.set_default_provider("faux")
    configs.replace_provider(
        _provider_id(configs),
        display_name="Subscription",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="http://localhost:1234",
    )
    provider_id = _provider_id(configs)
    configs.create_endpoint(provider_id, model_id="chat", make_default=True)
    configs.set_default_provider(provider_id)
    runtime = build_local_runtime(tmp_path, auth_drivers=drivers, service_policies=(policy,))

    async def quota(**request):
        requests.append(request)
        raise ProviderTransportError(
            "quota", retryable=True, status_code=429, service_error_code="fixture-quota"
        )

    monkeypatch.setattr(runtime.adapter.provider.transport, "post_json", quota)
    try:
        check = await runtime.adapter.provider.check_connection(runtime.adapter.model)
        assert check.error_code == "quota_exhausted"
        assert len(requests) == 1 and requests[0]["url"].startswith("http://localhost:1234/")
    finally:
        await runtime.close()
