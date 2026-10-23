from __future__ import annotations

import pytest

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.cli.main import main
from ouro.config import ProviderRuntimeOptions
from ouro.config.compaction import CompactionConfigManager
from ouro.config.endpoints import endpoint_config_from_mapping, endpoint_config_to_raw
from ouro.config.providers.runtime import merge_runtime_options
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata
from ouro.providers import build_runtime_adapter


@pytest.mark.parametrize("fields", [{}, {"max_output_tokens": None}])
def test_missing_and_null_output_limit_round_trip_as_unlimited(fields):
    endpoint = endpoint_config_from_mapping("test", {"model_id": "test", **fields})
    assert endpoint.max_output_tokens is None
    assert endpoint_config_from_mapping("test", endpoint_config_to_raw(endpoint)) == endpoint
    assert ModelMetadata(model_id="test", provider_id="test", **fields).max_output_tokens is None


def test_runtime_edit_preserves_unsubmitted_limit_and_can_explicitly_clear_it():
    base = ProviderRuntimeOptions(max_output_tokens=128)
    assert (
        merge_runtime_options(base, ProviderRuntimeOptions(temperature=0.3)).max_output_tokens
        == 128
    )
    assert (
        merge_runtime_options(
            base, ProviderRuntimeOptions(max_output_tokens=None)
        ).max_output_tokens
        is None
    )
    assert (
        merge_runtime_options(base, ProviderRuntimeOptions(max_output_tokens=256)).max_output_tokens
        == 256
    )


def test_factory_does_not_invent_an_output_limit():
    adapter = build_runtime_adapter(ProviderRuntimeOptions(protocol="faux"))
    assert adapter.model.max_output_tokens is None


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_output_limit_requires_a_positive_integer(limit):
    with pytest.raises(ValueError):
        endpoint_config_from_mapping("test", {"model_id": "test", "max_output_tokens": limit})
    with pytest.raises(ValueError):
        ModelMetadata(model_id="test", provider_id="test", max_output_tokens=limit)
    with pytest.raises(ValueError):
        ProviderRuntimeOptions(max_output_tokens=limit)


def test_endpoint_limit_can_be_cleared_without_affecting_unedited_fields(tmp_path):
    facade = ConfigurationFacade(tmp_path)
    provider_id = facade.add_provider(
        display_name="Test",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.test",
    ).changed_provider_id
    assert provider_id is not None
    endpoint_id = facade.add_endpoint(
        provider_id, model="test", max_output_tokens=100
    ).changed_endpoint_id
    assert endpoint_id is not None
    before = facade.endpoint(provider_id, endpoint_id)
    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        facade.update_endpoint(provider_id, endpoint_id, **{"max_output_token": 200})
    assert facade.endpoint(provider_id, endpoint_id) == before
    facade.update_endpoint(provider_id, endpoint_id, display_name="Renamed")
    assert facade.endpoint(provider_id, endpoint_id).max_output_tokens == 100
    assert (
        main(
            [
                "provider",
                "endpoint",
                "update",
                provider_id,
                endpoint_id,
                "--max-output-tokens",
                "none",
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    saved = ConfigurationFacade(tmp_path).endpoint(provider_id, endpoint_id)
    assert saved.max_output_tokens is None
    assert saved.display_name == "Renamed"
    assert saved.model_id == "test"


def test_summary_route_selection_preserves_its_independent_limit_and_explicit_null_clears(tmp_path):
    config = CompactionConfigManager(tmp_path)
    config.update_model(max_output_tokens=100)
    config.update_model(use_runtime_endpoint=True)
    assert config.snapshot().model.max_output_tokens == 100
    assert (
        main(
            [
                "config",
                "compaction",
                "--max-output-tokens",
                "none",
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    assert config.snapshot().model.max_output_tokens is None
