from __future__ import annotations

import json

import pytest

from ouro.config import ProviderConfigManager, ProviderEndpointConfig, ProviderTemplate
from ouro.core.errors import ConfigError


def _endpoint(
    endpoint_id: str,
    *,
    model_id: str | None = None,
    context_window: int = 100_000,
) -> ProviderEndpointConfig:
    return ProviderEndpointConfig(
        endpoint_id=endpoint_id,
        model_id=model_id or endpoint_id,
        context_window=context_window,
        supports_tools=True,
    )


def _template(
    revision: int,
    *endpoints: ProviderEndpointConfig,
    default_endpoint: str | None = None,
    base_url: str | None = None,
) -> ProviderTemplate:
    return ProviderTemplate(
        template_id="openai",
        display_name="OpenAI test preset",
        description="Template update behavior fixture.",
        provider_id="openai",
        provider_type="openai",
        protocol="openai-chat-completions",
        credential_env="OPENAI_API_KEY",
        base_url=base_url or f"https://api-v{revision}.example.test/v1",
        api_path="/chat/completions",
        default_endpoint=default_endpoint or endpoints[0].endpoint_id,
        endpoints=endpoints,
        documentation_url="https://example.test/docs",
        verified_on="2026-08-27",
        revision=revision,
    )


def test_template_update_merges_managed_values_and_preserves_user_ownership(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)
    original = _template(
        1,
        _endpoint("primary"),
        _endpoint("removed-managed"),
        _endpoint("removed-custom"),
    )
    manager.create_from_template(original, provider_id="account")
    raw = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))
    binding = raw["providers"]["items"]["account"]["template"]
    assert binding["template_id"] == "openai"
    assert set(binding["endpoints"][0]) == {
        "template_endpoint_id",
        "configured_endpoint_id",
        "fingerprint",
    }
    manager.upsert_endpoint("account", "primary", supports_vision=True)
    manager.upsert_endpoint("account", "removed-custom", model_id="private-model")
    manager.upsert_endpoint("account", "unknown", model_id="unknown-model")

    latest = _template(
        2,
        _endpoint("primary", context_window=200_000),
        _endpoint("new-default"),
        default_endpoint="new-default",
    )
    result = manager.update_from_template(latest, provider_id="account")
    provider = result.provider

    assert provider.base_url == "https://api-v2.example.test/v1"
    assert provider.default_endpoint == "new-default"
    assert {item.endpoint_id for item in provider.endpoints} == {
        "primary",
        "new-default",
        "removed-custom",
        "unknown",
    }
    assert provider.endpoint("primary").context_window == 100_000
    assert provider.endpoint("primary").supports_vision is True
    assert provider.endpoint("removed-custom").model_id == "private-model"
    assert result.added_endpoints == ("new-default",)
    assert result.removed_endpoints == ("removed-managed",)
    assert set(result.preserved_endpoints) == {
        "primary",
        "removed-custom",
    }
    assert result.updated_fields == ("base_url", "default_endpoint")
    assert provider.template is not None and provider.template.revision == 2

    next_release = _template(
        3,
        _endpoint("primary", context_window=300_000),
        _endpoint("new-default", context_window=300_000),
        default_endpoint="new-default",
    )
    second = manager.update_from_template(next_release, provider_id="account")

    assert second.provider.endpoint("primary").context_window == 100_000
    assert second.provider.endpoint("new-default").context_window == 300_000
    assert second.updated_endpoints == ("new-default",)
    assert second.preserved_endpoints == ("primary",)


def test_template_update_respects_local_deletion_and_reports_new_endpoint_collision(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)
    original = _template(1, _endpoint("primary"), _endpoint("optional"))
    manager.create_from_template(original, provider_id="account")
    manager.remove_endpoint("account", "optional")
    manager.upsert_endpoint("account", "collision", model_id="user-model")

    latest = _template(
        2,
        _endpoint("primary"),
        _endpoint("optional", context_window=200_000),
        _endpoint("collision", model_id="template-model"),
    )
    result = manager.update_from_template(latest, provider_id="account")

    assert result.provider.endpoint("optional") is None
    assert result.provider.endpoint("collision").model_id == "user-model"
    assert result.preserved_endpoints == ("optional",)
    assert result.conflicted_endpoints == ("collision",)
    assert result.provider.template is not None
    assert result.provider.template.endpoint_binding("optional") is not None
    assert result.provider.template.endpoint_binding("collision") is None


def test_template_update_preserves_custom_provider_fields_and_removed_user_default(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)
    original = _template(1, _endpoint("primary"), _endpoint("alternate"))
    manager.create_from_template(original, provider_id="account")
    manager.update_provider(
        "account",
        base_url="https://gateway.example.test/v1",
        api_path="/custom/chat",
        api_key_env="PRIVATE_KEY",
    )
    manager.set_default_route("account", "alternate")
    latest = _template(2, _endpoint("primary")).model_copy(
        update={
            "credential_env": "NEW_OPENAI_KEY",
            "api_path": "/v2/chat/completions",
        }
    )

    result = manager.update_from_template(latest, provider_id="account")

    assert result.provider.base_url == "https://gateway.example.test/v1"
    assert result.provider.api_path == "/custom/chat"
    assert result.provider.credential_ref.display() == "env:PRIVATE_KEY"
    assert result.provider.default_endpoint == "alternate"
    assert result.provider.endpoint("alternate") is not None
    assert set(result.preserved_fields) == {
        "api_path",
        "base_url",
        "credential",
    }
    assert result.preserved_endpoints == ("alternate",)


def test_template_update_keeps_configured_endpoint_id_mapping(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    manager.create_from_template(
        _template(1, _endpoint("primary"), _endpoint("secondary")),
        provider_id="account",
        endpoint_id="stable-alias",
    )

    result = manager.update_from_template(
        _template(
            2,
            _endpoint("primary", context_window=200_000),
            _endpoint("secondary"),
        ),
        provider_id="account",
    )

    assert result.updated_endpoints == ("stable-alias",)
    assert result.provider.endpoint("primary") is None
    assert result.provider.endpoint("stable-alias").context_window == 200_000
    assert result.provider.template is not None
    binding = result.provider.template.endpoint_binding("primary")
    assert binding is not None and binding.configured_endpoint_id == "stable-alias"

    renamed = manager.update_from_template(
        _template(
            3,
            _endpoint("primary", context_window=300_000),
            _endpoint("secondary"),
        ),
        provider_id="account",
        endpoint_id="new-alias",
    )
    assert renamed.updated_endpoints == ("new-alias",)
    assert renamed.provider.endpoint("stable-alias") is None
    assert renamed.provider.endpoint("new-alias").context_window == 300_000


def test_update_requires_template_provenance_without_rewriting_provider(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    manager.create_provider(
        "manual", provider_type="openai", base_url="https://gateway.example.test/v1"
    )
    manager.upsert_endpoint("manual", "primary", model_id="user-model", make_default=True)
    path = tmp_path / "config" / "config.json"
    before = path.read_bytes()

    with pytest.raises(ConfigError, match="no template provenance"):
        manager.update_from_template(
            _template(2, _endpoint("primary"), _endpoint("new")), provider_id="manual"
        )

    assert path.read_bytes() == before


def test_template_source_change_is_rejected_without_rewriting_provider(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    template = _template(1, _endpoint("primary"))
    manager.create_from_template(
        template,
        template_origin="inline",
        provider_id="account",
    )
    path = tmp_path / "config" / "config.json"
    before = json.loads(path.read_text(encoding="utf-8"))

    with pytest.raises(ConfigError, match="template source changed"):
        manager.update_from_template(
            template,
            template_origin="custom",
            provider_id="account",
        )

    assert json.loads(path.read_text(encoding="utf-8")) == before
