from ouro.application.models import ConfiguredModelCatalog
from ouro.config import ProviderConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata
from ouro.providers import FauxProvider, OpenAICompatibleProvider, build_runtime_adapter


def test_configured_model_catalog_lists_enabled_endpoints_and_builds_adapter(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    configs.upsert_provider(
        "openai",
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
        "faux/faux",
        "openai/fast",
        "openai/smart",
    ]
    assert catalog.resolve("gpt-smart").key == "openai/smart"

    adapter = catalog.adapter_for("openai/fast")

    assert isinstance(adapter.provider, OpenAICompatibleProvider)
    assert adapter.model.provider_id == "openai"
    assert adapter.model.endpoint_id == "fast"
    assert adapter.model.model_id == "gpt-fast"


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

    assert [model.key for model in catalog.list_models()] == ["faux/faux"]

    configs.upsert_provider("local", base_url="http://localhost:1234/v1")
    configs.upsert_endpoint("local", "coder", model_id="local-coder")

    assert [model.key for model in catalog.list_models()] == [
        "faux/faux",
        "local/coder",
    ]


def test_configured_model_catalog_preserves_runtime_overrides_for_default_route(
    tmp_path,
) -> None:
    configs = ProviderConfigManager(tmp_path)
    configs.upsert_provider("openai", base_url="https://configured.example/v1")
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
