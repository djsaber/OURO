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


@pytest.mark.parametrize("previous_env", (None, "OPENAI_API_KEY"))
def test_template_update_does_not_bind_unconfigured_credentials(tmp_path, previous_env):
    manager = ProviderConfigManager(tmp_path)
    original = _template(1, _endpoint("primary")).model_copy(
        update={"credential_env": previous_env}
    )
    provider = manager.create_from_template(original)
    assert provider.credential_ref.type == "none"
    latest = _template(2, _endpoint("primary")).model_copy(update={"credential_env": "NEW_KEY"})
    updated = manager.update_from_template(latest, provider_id=provider.provider_id)
    assert updated.provider.credential_ref.type == "none"


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
    created = manager.create_from_template(original)
    provider_id = created.provider_id
    assert created.template is not None
    primary_id = created.template.endpoint_binding("primary").configured_endpoint_id
    removed_managed_id = created.template.endpoint_binding("removed-managed").configured_endpoint_id
    removed_custom_id = created.template.endpoint_binding("removed-custom").configured_endpoint_id
    raw = json.loads((tmp_path / "config" / "providers.json").read_text(encoding="utf-8"))
    binding = raw["providers"]["items"][provider_id]["template"]
    assert binding["template_id"] == "openai"
    assert set(binding["endpoints"][0]) == {
        "template_endpoint_id",
        "configured_endpoint_id",
        "fingerprint",
    }
    manager.update_endpoint(
        provider_id,
        primary_id,
        display_name=None,
        supports_image_input=True,
    )
    manager.update_endpoint(
        provider_id,
        removed_custom_id,
        display_name=None,
        model_id="private-model",
    )
    unknown = manager.create_endpoint(provider_id, model_id="unknown-model")

    latest = _template(
        2,
        _endpoint("primary", context_window=200_000),
        _endpoint("new-default"),
        default_endpoint="new-default",
    )
    result = manager.update_from_template(latest, provider_id=provider_id)
    provider = result.provider
    assert provider.template is not None
    new_default_id = provider.template.endpoint_binding("new-default").configured_endpoint_id

    assert provider.base_url == "https://api-v2.example.test/v1"
    assert provider.default_endpoint == new_default_id
    assert {item.endpoint_id for item in provider.endpoints} == {
        primary_id,
        new_default_id,
        removed_custom_id,
        unknown.endpoint_id,
    }
    assert provider.endpoint(primary_id).context_window == 100_000
    assert provider.endpoint(primary_id).supports_image_input is True
    assert provider.endpoint(removed_custom_id).model_id == "private-model"
    assert result.added_endpoints == (new_default_id,)
    assert result.removed_endpoints == (removed_managed_id,)
    assert set(result.preserved_endpoints) == {
        primary_id,
        removed_custom_id,
    }
    assert result.updated_fields == ("base_url", "default_endpoint")
    assert provider.template is not None and provider.template.revision == 2

    next_release = _template(
        3,
        _endpoint("primary", context_window=300_000),
        _endpoint("new-default", context_window=300_000),
        default_endpoint="new-default",
    )
    second = manager.update_from_template(next_release, provider_id=provider_id)

    assert second.provider.endpoint(primary_id).context_window == 100_000
    assert second.provider.endpoint(new_default_id).context_window == 300_000
    assert second.updated_endpoints == (new_default_id,)
    assert second.preserved_endpoints == (primary_id,)


def test_template_update_respects_local_deletion_and_keeps_user_endpoint(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)
    original = _template(1, _endpoint("primary"), _endpoint("optional"))
    created = manager.create_from_template(original)
    provider_id = created.provider_id
    assert created.template is not None
    optional_id = created.template.endpoint_binding("optional").configured_endpoint_id
    manager.remove_endpoint(provider_id, optional_id)
    user_endpoint = manager.create_endpoint(provider_id, model_id="user-model")

    latest = _template(
        2,
        _endpoint("primary"),
        _endpoint("optional", context_window=200_000),
        _endpoint("collision", model_id="template-model"),
    )
    result = manager.update_from_template(latest, provider_id=provider_id)

    assert result.provider.endpoint(optional_id) is None
    assert result.provider.endpoint(user_endpoint.endpoint_id).model_id == "user-model"
    assert result.preserved_endpoints == (optional_id,)
    assert result.conflicted_endpoints == ()
    assert result.provider.template is not None
    assert result.provider.template.endpoint_binding("optional") is not None
    collision = result.provider.template.endpoint_binding("collision")
    assert collision is not None
    assert collision.configured_endpoint_id != user_endpoint.endpoint_id


def test_template_update_preserves_custom_provider_fields_and_removed_user_default(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)
    original = _template(1, _endpoint("primary"), _endpoint("alternate"))
    created = manager.create_from_template(original)
    provider_id = created.provider_id
    assert created.template is not None
    alternate_id = created.template.endpoint_binding("alternate").configured_endpoint_id
    manager.update_provider(
        provider_id,
        base_url="https://gateway.example.test/v1",
        api_path="/custom/chat",
        api_key_env="PRIVATE_KEY",
    )
    manager.set_default_route(provider_id, alternate_id)
    latest = _template(2, _endpoint("primary")).model_copy(
        update={
            "credential_env": "NEW_OPENAI_KEY",
            "api_path": "/v2/chat/completions",
        }
    )

    result = manager.update_from_template(latest, provider_id=provider_id)

    assert result.provider.base_url == "https://gateway.example.test/v1"
    assert result.provider.api_path == "/custom/chat"
    assert result.provider.credential_ref.display() == "env:PRIVATE_KEY"
    assert result.provider.default_endpoint == alternate_id
    assert result.provider.endpoint(alternate_id) is not None
    assert set(result.preserved_fields) == {
        "api_path",
        "base_url",
        "credential",
    }
    assert result.preserved_endpoints == (alternate_id,)


def test_template_update_keeps_generated_endpoint_id_mapping(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    created = manager.create_from_template(
        _template(1, _endpoint("primary"), _endpoint("secondary"))
    )
    assert created.template is not None
    primary_id = created.template.endpoint_binding("primary").configured_endpoint_id

    result = manager.update_from_template(
        _template(
            2,
            _endpoint("primary", context_window=200_000),
            _endpoint("secondary"),
        ),
        provider_id=created.provider_id,
    )

    assert result.updated_endpoints == (primary_id,)
    assert result.provider.endpoint("primary") is None
    assert result.provider.endpoint(primary_id).context_window == 200_000
    assert result.provider.template is not None
    binding = result.provider.template.endpoint_binding("primary")
    assert binding is not None and binding.configured_endpoint_id == primary_id

    renamed = manager.update_from_template(
        _template(
            3,
            _endpoint("primary", context_window=300_000),
            _endpoint("secondary"),
        ),
        provider_id=created.provider_id,
    )
    assert renamed.updated_endpoints == (primary_id,)
    assert renamed.provider.endpoint(primary_id).context_window == 300_000


def test_update_requires_template_provenance_without_rewriting_provider(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_provider(
        display_name="Manual",
        provider_type="openai",
        protocol="openai-chat-completions",
        base_url="https://gateway.example.test/v1",
    )
    manager.create_endpoint(provider.provider_id, model_id="user-model", make_default=True)
    path = tmp_path / "config" / "providers.json"
    before = path.read_bytes()

    with pytest.raises(ConfigError, match="no template provenance"):
        manager.update_from_template(
            _template(2, _endpoint("primary"), _endpoint("new")),
            provider_id=provider.provider_id,
        )

    assert path.read_bytes() == before


def test_template_source_change_is_rejected_without_rewriting_provider(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    template = _template(1, _endpoint("primary"))
    created = manager.create_from_template(
        template,
        template_origin="inline",
    )
    path = tmp_path / "config" / "providers.json"
    before = json.loads(path.read_text(encoding="utf-8"))

    with pytest.raises(ConfigError, match="template source changed"):
        manager.update_from_template(
            template,
            template_origin="custom",
            provider_id=created.provider_id,
        )

    assert json.loads(path.read_text(encoding="utf-8")) == before
