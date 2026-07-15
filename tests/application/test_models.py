from ouro.application.models import ConfiguredModelCatalog
from ouro.config import ProviderConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata
from ouro.providers import FauxProvider, OpenAICompatibleProvider, build_runtime_adapter


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
