import asyncio
import time

import pytest

from ouro.application.models.catalog import ConfiguredModelCatalog
from ouro.config import ProviderConfigManager, ProviderRuntimeOptions
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata
from ouro.providers import FauxProvider, build_runtime_adapter
from ouro.providers.openai_responses.adapter import OpenAIResponsesProvider


def _add_provider(configs, display_name, **options):
    return configs.create_provider(display_name=display_name, **options).provider_id


def _add_endpoint(configs, provider_id, model_id, **options):
    return configs.create_endpoint(provider_id, model_id=model_id, **options).endpoint_id


def _key(provider_id, endpoint_id):
    return f"{provider_id}/{endpoint_id}"


async def test_header_declarations_invalidate_route_cache_and_preserve_inflight_adapter(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs,
        "Service",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://model.test",
        headers={"X-Tenant": {"source": "literal", "value": "first"}},
    )
    endpoint_id = _add_endpoint(configs, provider_id, "model", make_default=True)
    configs.set_default_provider(provider_id)
    selector = _key(provider_id, endpoint_id)
    original = build_runtime_adapter(configs.resolve_runtime_options(ProviderRuntimeOptions()))
    catalog = ConfiguredModelCatalog(configs, original, adapter_builder=build_runtime_adapter)
    try:
        assert catalog.adapter_for(selector) is original
        configs.update_provider(
            provider_id, headers={"X-Tenant": {"source": "literal", "value": "second"}}
        )
        current = catalog.adapter_for(selector)
        assert current is not original
        assert catalog.adapter_for(selector) is current
        assert original.provider.header_resolver.resolve(None, defaults={})["x-tenant"] == "first"
        assert current.provider.header_resolver.resolve(None, defaults={})["x-tenant"] == "second"
    finally:
        await catalog.close()


async def test_default_adapter_rebinds_connection_and_inline_credential_changes(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs,
        "Service",
        provider_type="openai",
        base_url="https://first.example/v1",
        api_key="first",
    )
    endpoint_id = _add_endpoint(configs, provider_id, "model", make_default=True)
    configs.set_default_provider(provider_id)
    selector = _key(provider_id, endpoint_id)
    default = build_runtime_adapter(configs.resolve_runtime_options(ProviderRuntimeOptions()))
    catalog = ConfiguredModelCatalog(configs, default, adapter_builder=build_runtime_adapter)
    try:
        assert catalog.adapter_for(selector) is default
        configs.update_provider(provider_id, api_key="second")
        second = catalog.adapter_for(selector)
        assert second is not default
        auth = await second.provider.authenticator.acquire("https://first.example/v1/responses")
        assert auth.headers["authorization"] == "Bearer second"
        original_auth = await default.provider.authenticator.acquire(
            "https://first.example/v1/responses"
        )
        assert original_auth.headers["authorization"] == "Bearer first"
        configs.update_provider(provider_id, base_url="https://second.example/v1")
        third = catalog.adapter_for(selector)
        assert third is not second
        assert third.provider.base_url == "https://second.example/v1"
        assert catalog.adapter_for(selector) is third
    finally:
        await catalog.close()


async def test_default_rebind_preserves_explicit_overrides_and_uses_actual_startup_snapshot(
    tmp_path,
):
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs, "Service", provider_type="openai", base_url="https://first.example/v1"
    )
    endpoint_id = _add_endpoint(configs, provider_id, "one", make_default=True)
    configs.set_default_provider(provider_id)
    selector = _key(provider_id, endpoint_id)
    overrides = ProviderRuntimeOptions(temperature=0.25, api_key="override")
    options = configs.resolve_runtime_options(overrides)
    default = build_runtime_adapter(options)
    # A configuration write between snapshot and catalog assembly must not mark
    # an adapter built from the older snapshot as current.
    configs.update_endpoint(
        provider_id,
        endpoint_id,
        display_name=None,
        model_id="two",
        temperature=0.75,
    )
    catalog = ConfiguredModelCatalog(
        configs,
        default,
        adapter_builder=build_runtime_adapter,
        default_options=options,
        default_overrides=overrides,
    )
    try:
        current = catalog.adapter_for(selector)
        assert current is not default and current.model.model_id == "two"
        assert current.model.temperature == 0.25
        auth = await current.provider.authenticator.acquire("https://first.example/v1/responses")
        assert auth.headers["authorization"] == "Bearer override"
        configs.set_default_provider("faux")
        configs.update_provider(provider_id, enabled=False)
        with pytest.raises(ConfigError, match="unavailable"):
            catalog.adapter_for(selector)
    finally:
        await catalog.close()


async def test_nondefault_inline_key_change_rebuilds_adapter_despite_redacted_repr(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs,
        "Service",
        provider_type="openai",
        base_url="https://first.example/v1",
        api_key="first",
    )
    endpoint_id = _add_endpoint(configs, provider_id, "model", make_default=True)
    selector = _key(provider_id, endpoint_id)
    default = RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux"))
    catalog = ConfiguredModelCatalog(configs, default, adapter_builder=build_runtime_adapter)
    try:
        initial = catalog.adapter_for(selector)
        configs.update_provider(provider_id, api_key="second")
        latest = catalog.adapter_for(selector)
        assert latest is not initial
        auth = await latest.provider.authenticator.acquire("https://first.example/v1/responses")
        assert auth.headers["authorization"] == "Bearer second"
    finally:
        await catalog.close()


async def test_concurrent_model_resolution_publishes_one_owned_adapter(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs, "Service", provider_type="openai", base_url="https://model.example/v1"
    )
    endpoint_id = _add_endpoint(configs, provider_id, "model")
    selector = _key(provider_id, endpoint_id)
    default = RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux"))
    built = []

    def build(options):
        time.sleep(0.03)
        adapter = build_runtime_adapter(options)
        built.append(adapter)
        return adapter

    catalog = ConfiguredModelCatalog(configs, default, adapter_builder=build)
    try:
        adapters = await asyncio.gather(
            *(asyncio.to_thread(catalog.adapter_for, selector) for _ in range(5))
        )
        assert len(built) == 1 and all(adapter is built[0] for adapter in adapters)
    finally:
        await catalog.close()
    with pytest.raises(RuntimeError, match="closed"):
        catalog.adapter_for(selector)


async def test_removal_during_startup_does_not_turn_a_configured_default_into_ad_hoc(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs, "Service", provider_type="openai", base_url="https://model.example/v1"
    )
    endpoint_id = _add_endpoint(configs, provider_id, "model", make_default=True)
    selector = _key(provider_id, endpoint_id)
    options = configs.resolve_runtime_options(ProviderRuntimeOptions(provider_id=provider_id))
    default = build_runtime_adapter(options)
    configs.remove_provider(provider_id)
    catalog = ConfiguredModelCatalog(
        configs, default, adapter_builder=build_runtime_adapter, default_options=options
    )
    try:
        with pytest.raises(ConfigError, match="unavailable"):
            catalog.adapter_for(selector)
    finally:
        await catalog.close()


def test_configured_model_catalog_lists_enabled_endpoints_and_builds_adapter(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs,
        "OpenAI",
        provider_type="openai",
        base_url="https://models.example/v1",
        api_key="test-key",
    )
    fast_id = _add_endpoint(configs, provider_id, "gpt-fast", display_name="Fast")
    smart_id = _add_endpoint(configs, provider_id, "gpt-smart", display_name="Smart")
    fast_key = _key(provider_id, fast_id)
    smart_key = _key(provider_id, smart_id)
    default_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(model_id="faux", provider_id="faux", endpoint_id="faux"),
    )
    catalog = ConfiguredModelCatalog(
        configs,
        default_adapter,
        adapter_builder=build_runtime_adapter,
    )

    assert [model.key for model in catalog.list_models()] == sorted([fast_key, smart_key])
    assert catalog.resolve(smart_key).key == smart_key
    assert catalog.resolve(smart_key).provider_display_name == "OpenAI"
    assert catalog.resolve(smart_key).display_name == "Smart"

    adapter = catalog.adapter_for(fast_key)

    assert isinstance(adapter.provider, OpenAIResponsesProvider)
    assert adapter.provider.api_path == "/responses"
    assert adapter.model.provider_id == provider_id
    assert adapter.model.endpoint_id == fast_id
    assert adapter.model.model_id == "gpt-fast"
    assert catalog.adapter_for(fast_key) is adapter

    configs.update_endpoint(
        provider_id,
        fast_id,
        display_name="Fast",
        temperature=0.5,
    )
    refreshed = catalog.adapter_for(fast_key)
    assert refreshed is not adapter
    assert refreshed.model.temperature == 0.5


def test_configured_model_catalog_reflects_config_changes_without_restart(tmp_path) -> None:
    configs = ProviderConfigManager(tmp_path)
    default_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(model_id="faux", provider_id="faux", endpoint_id="faux"),
    )
    catalog = ConfiguredModelCatalog(
        configs,
        default_adapter,
        adapter_builder=build_runtime_adapter,
    )

    assert catalog.list_models() == []

    provider_id = _add_provider(
        configs,
        "Local",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="http://localhost:1234/v1",
    )
    endpoint_id = _add_endpoint(configs, provider_id, "local-coder")

    assert [model.key for model in catalog.list_models()] == [_key(provider_id, endpoint_id)]


def test_configured_model_catalog_default_model_follows_default_route_changes(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs,
        "OpenAI",
        provider_type="openai",
        base_url="https://configured.example/v1",
    )
    first_id = _add_endpoint(configs, provider_id, "gpt-first", make_default=True)
    second_id = _add_endpoint(configs, provider_id, "gpt-second")
    configs.set_default_route(provider_id, first_id)
    runtime_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(model_id="gpt-first", provider_id=provider_id, endpoint_id=first_id),
    )
    catalog = ConfiguredModelCatalog(
        configs,
        runtime_adapter,
        adapter_builder=build_runtime_adapter,
    )

    assert catalog.default_model().key == _key(provider_id, first_id)

    configs.set_default_route(provider_id, second_id)

    assert catalog.default_model().key == _key(provider_id, second_id)


def test_configured_model_catalog_pins_default_route_composed_outside_config(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs,
        "OpenAI",
        provider_type="openai",
        base_url="https://configured.example/v1",
    )
    first_id = _add_endpoint(configs, provider_id, "gpt-first", make_default=True)
    second_id = _add_endpoint(configs, provider_id, "gpt-second")
    configs.set_default_route(provider_id, first_id)
    runtime_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(model_id="ad-hoc", provider_id="custom", endpoint_id="route"),
    )
    catalog = ConfiguredModelCatalog(
        configs,
        runtime_adapter,
        adapter_builder=build_runtime_adapter,
    )

    assert catalog.default_model().key == "custom/route"

    configs.set_default_route(provider_id, second_id)

    assert catalog.default_model().key == "custom/route"


def test_configured_model_catalog_preserves_runtime_overrides_for_default_route(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    provider_id = _add_provider(
        configs,
        "OpenAI",
        provider_type="openai",
        base_url="https://configured.example/v1",
    )
    endpoint_id = _add_endpoint(configs, provider_id, "gpt")
    runtime_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(
            model_id="gpt",
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            temperature=0.25,
        ),
    )
    catalog = ConfiguredModelCatalog(
        configs,
        runtime_adapter,
        adapter_builder=build_runtime_adapter,
    )

    assert catalog.adapter_for(_key(provider_id, endpoint_id)) is runtime_adapter
