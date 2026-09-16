import asyncio
import time

import pytest

from ouro.application.models import ConfiguredModelCatalog
from ouro.config import ProviderConfigManager, ProviderRuntimeOptions
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata
from ouro.providers import FauxProvider, OpenAICompatibleProvider, build_runtime_adapter


async def test_default_adapter_rebinds_connection_and_inline_credential_changes(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider(
        "service", provider_type="openai", base_url="https://first.example/v1", api_key="first"
    )
    configs.upsert_endpoint("service", "chat", model_id="model", make_default=True)
    configs.set_default_provider("service")
    default = build_runtime_adapter(configs.resolve_runtime_options(ProviderRuntimeOptions()))
    catalog = ConfiguredModelCatalog(configs, default, adapter_builder=build_runtime_adapter)
    try:
        assert catalog.adapter_for("service/chat") is default
        configs.update_provider("service", api_key="second")
        second = catalog.adapter_for("service/chat")
        assert second is not default
        assert second.provider.api_key == "second"
        configs.update_provider("service", base_url="https://second.example/v1")
        third = catalog.adapter_for("service/chat")
        assert third is not second
        assert third.provider.base_url == "https://second.example/v1"
        assert catalog.adapter_for("service/chat") is third
    finally:
        await catalog.close()


async def test_default_rebind_preserves_explicit_overrides_and_uses_actual_startup_snapshot(
    tmp_path,
):
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider("service", provider_type="openai", base_url="https://first.example/v1")
    configs.upsert_endpoint("service", "chat", model_id="one", make_default=True)
    configs.set_default_provider("service")
    overrides = ProviderRuntimeOptions(temperature=0.25, api_key="override")
    options = configs.resolve_runtime_options(overrides)
    default = build_runtime_adapter(options)
    # A configuration write between snapshot and catalog assembly must not mark
    # an adapter built from the older snapshot as current.
    configs.upsert_endpoint("service", "chat", model_id="two", temperature=0.75)
    catalog = ConfiguredModelCatalog(
        configs,
        default,
        adapter_builder=build_runtime_adapter,
        default_options=options,
        default_overrides=overrides,
    )
    try:
        current = catalog.adapter_for("service/chat")
        assert current is not default and current.model.model_id == "two"
        assert current.model.temperature == 0.25 and current.provider.api_key == "override"
        configs.update_provider("service", enabled=False)
        with pytest.raises(ConfigError, match="unavailable"):
            catalog.adapter_for("service/chat")
    finally:
        await catalog.close()


async def test_nondefault_inline_key_change_rebuilds_adapter_despite_redacted_repr(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider(
        "service", provider_type="openai", base_url="https://first.example/v1", api_key="first"
    )
    configs.upsert_endpoint("service", "chat", model_id="model", make_default=True)
    default = RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux"))
    catalog = ConfiguredModelCatalog(configs, default, adapter_builder=build_runtime_adapter)
    try:
        initial = catalog.adapter_for("service/chat")
        configs.update_provider("service", api_key="second")
        latest = catalog.adapter_for("service/chat")
        assert latest is not initial and latest.provider.api_key == "second"
    finally:
        await catalog.close()


async def test_concurrent_model_resolution_publishes_one_owned_adapter(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider("service", provider_type="openai", base_url="https://model.example/v1")
    configs.upsert_endpoint("service", "chat", model_id="model")
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
            *(asyncio.to_thread(catalog.adapter_for, "service/chat") for _ in range(5))
        )
        assert len(built) == 1 and all(adapter is built[0] for adapter in adapters)
    finally:
        await catalog.close()
    with pytest.raises(RuntimeError, match="closed"):
        catalog.adapter_for("service/chat")


async def test_removal_during_startup_does_not_turn_a_configured_default_into_ad_hoc(tmp_path):
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider("service", provider_type="openai", base_url="https://model.example/v1")
    configs.upsert_endpoint("service", "chat", model_id="model", make_default=True)
    options = configs.resolve_runtime_options(ProviderRuntimeOptions(provider_id="service"))
    default = build_runtime_adapter(options)
    configs.remove_provider("service")
    catalog = ConfiguredModelCatalog(
        configs, default, adapter_builder=build_runtime_adapter, default_options=options
    )
    try:
        with pytest.raises(ConfigError, match="unavailable"):
            catalog.adapter_for("service/chat")
    finally:
        await catalog.close()


def test_configured_model_catalog_lists_enabled_endpoints_and_builds_adapter(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider(
        "openai",
        provider_type="openai",
        base_url="https://models.example/v1",
        api_key="test-key",
    )
    configs.upsert_endpoint("openai", "fast", model_id="gpt-fast")
    configs.upsert_endpoint("openai", "smart", model_id="gpt-smart")
    default_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(model_id="faux", provider_id="faux", endpoint_id="faux"),
    )
    catalog = ConfiguredModelCatalog(
        configs,
        default_adapter,
        adapter_builder=build_runtime_adapter,
    )

    assert [model.key for model in catalog.list_models()] == [
        "openai/fast",
        "openai/smart",
    ]
    assert catalog.resolve("openai/smart").key == "openai/smart"

    adapter = catalog.adapter_for("openai/fast")

    assert isinstance(adapter.provider, OpenAICompatibleProvider)
    assert adapter.model.provider_id == "openai"
    assert adapter.model.endpoint_id == "fast"
    assert adapter.model.model_id == "gpt-fast"
    assert catalog.adapter_for("openai/fast") is adapter

    configs.upsert_endpoint("openai", "fast", temperature=0.5)
    refreshed = catalog.adapter_for("openai/fast")
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

    configs.create_provider(
        "local",
        provider_type="openai-compatible",
        base_url="http://localhost:1234/v1",
    )
    configs.upsert_endpoint("local", "coder", model_id="local-coder")

    assert [model.key for model in catalog.list_models()] == ["local/coder"]


def test_configured_model_catalog_default_model_follows_default_route_changes(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider(
        "openai",
        provider_type="openai",
        base_url="https://configured.example/v1",
    )
    configs.upsert_endpoint("openai", "first", model_id="gpt-first", make_default=True)
    configs.upsert_endpoint("openai", "second", model_id="gpt-second")
    configs.set_default_route("openai", "first")
    runtime_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(model_id="gpt-first", provider_id="openai", endpoint_id="first"),
    )
    catalog = ConfiguredModelCatalog(
        configs,
        runtime_adapter,
        adapter_builder=build_runtime_adapter,
    )

    assert catalog.default_model().key == "openai/first"

    configs.set_default_route("openai", "second")

    assert catalog.default_model().key == "openai/second"


def test_configured_model_catalog_pins_default_route_composed_outside_config(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider(
        "openai",
        provider_type="openai",
        base_url="https://configured.example/v1",
    )
    configs.upsert_endpoint("openai", "first", model_id="gpt-first", make_default=True)
    configs.upsert_endpoint("openai", "second", model_id="gpt-second")
    configs.set_default_route("openai", "first")
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

    configs.set_default_route("openai", "second")

    assert catalog.default_model().key == "custom/route"


def test_configured_model_catalog_preserves_runtime_overrides_for_default_route(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    configs.create_provider(
        "openai",
        provider_type="openai",
        base_url="https://configured.example/v1",
    )
    configs.upsert_endpoint("openai", "default", model_id="gpt")
    runtime_adapter = RuntimeAdapter(
        FauxProvider(),
        ModelMetadata(
            model_id="gpt",
            provider_id="openai",
            endpoint_id="default",
            temperature=0.25,
        ),
    )
    catalog = ConfiguredModelCatalog(
        configs,
        runtime_adapter,
        adapter_builder=build_runtime_adapter,
    )

    assert catalog.adapter_for("openai/default") is runtime_adapter
