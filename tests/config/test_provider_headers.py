from __future__ import annotations

import json

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.config import ProviderConfigManager, ProviderRuntimeOptions
from ouro.config.provider_headers import parse_provider_headers, public_provider_headers
from ouro.core.errors import ConfigError
from ouro.core.request_context import ProviderRequestContext

HEADERS = {
    "X-Private": {"source": "literal", "value": "private-value"},
    "X-Tenant": {"source": "env", "name": "TEST_TENANT"},
    "X-Context": {"source": "context", "key": "session.id"},
}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"": {"source": "literal", "value": "secret"}},
        {"X Bad": {"source": "literal", "value": "secret"}},
        {"X-Good\r\nInjected": {"source": "literal", "value": "secret"}},
        {
            "X-One": {"source": "literal", "value": "1"},
            "x-one": {"source": "literal", "value": "2"},
        },
        {"X-One": {"source": "literal", "value": 123}},
        {"X-One": {"source": "literal", "value": "secret\nvalue"}},
        {"X-One": {"source": "literal", "value": "中文"}},
        {"X-One": {"source": "literal", "value": " secret"}},
        {"X-One": {"source": "literal", "value": ""}},
        {"X-One": {"source": "literal", "value": "secret", "key": "session.id"}},
        {"X-One": {"source": "env", "name": "INVALID NAME"}},
        {"X-One": {"source": "context", "key": "session.private_token"}},
        {"X-One": {"source": "secret"}},
        {"X-One": "secret"},
        {"X-One": {"value": "secret"}},
    ],
)
def test_invalid_declarations_fail_without_echoing_values(raw):
    with pytest.raises(ValueError) as error:
        parse_provider_headers(raw)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "name",
    [
        "Host",
        "Content-Length",
        "TRANSFER-ENCODING",
        "Connection",
        "Accept",
        "Content-Type",
        "Content-Encoding",
        "Anthropic-Version",
    ],
)
def test_wire_owned_headers_are_rejected(name):
    with pytest.raises(ValueError, match="owned"):
        parse_provider_headers({name: {"source": "literal", "value": "anything"}})


def test_sources_are_explicit_and_values_are_not_in_repr_or_public_projection():
    headers = parse_provider_headers(HEADERS)
    assert "private-value" not in repr(headers)
    public = public_provider_headers(headers)
    assert public["x-private"] == {"source": "literal", "value": "***"}
    assert public["x-tenant"] == HEADERS["X-Tenant"]
    assert public["x-context"] == HEADERS["X-Context"]
    assert set(headers) == {"x-private", "x-tenant", "x-context"}


def test_context_copies_values_and_rejects_unregistered_fields():
    values = {"session.id": "one"}
    context = ProviderRequestContext(values)
    values["session.id"] = "two"
    assert context.values["session.id"] == "one"
    assert "one" not in repr(context)
    with pytest.raises(TypeError):
        context.values["session.id"] = "three"
    with pytest.raises(ValueError, match="unregistered"):
        ProviderRequestContext({"unknown": "secret"})


@pytest.mark.parametrize("provider_type", ["custom", "openai"])
def test_configuration_round_trip_preserve_replace_clear_and_public_redaction(
    tmp_path, provider_type
):
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider(
        "service",
        provider_type=provider_type,
        protocol="openai-responses",
        base_url="https://model.test",
        headers=HEADERS,
    )
    configs = ProviderConfigManager(tmp_path)
    if provider_type == "custom":
        facade.save_endpoint("service", "chat", model="model")
    provider = configs.get_provider("service")
    assert provider.headers["x-private"].value == "private-value"
    assert "private-value" in configs.config_path.read_text(encoding="utf-8")
    assert "private-value" not in facade.provider("service").model_dump_json()
    options = configs.resolve_runtime_options(ProviderRuntimeOptions(provider_id="service"))
    assert options.headers == provider.headers
    assert "private-value" not in repr(options)

    # This includes the integrated provider's explicit template synchronization path.
    facade.update_provider("service", timeout=23)
    assert configs.get_provider("service").headers == provider.headers
    before = configs.config_path.read_bytes()
    with pytest.raises(ConfigError):
        facade.update_provider(
            "service", headers={"X-Bad": {"source": "literal", "value": "secret\n"}}
        )
    assert configs.config_path.read_bytes() == before
    facade.update_provider_settings(
        "service", headers={"X-New": {"source": "literal", "value": "new"}}
    )
    assert set(configs.get_provider("service").headers) == {"x-new"}
    facade.update_provider("service", headers={})
    assert configs.get_provider("service").headers == {}
    assert (
        "headers"
        not in json.loads(configs.config_path.read_text())["providers"]["items"]["service"]
    )


def test_runtime_header_override_is_whole_map_and_empty_clears(tmp_path):
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider(
        "service",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://model.test",
        headers=HEADERS,
    )
    facade.save_endpoint("service", "chat", model="model")
    options = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="service", headers={})
    )
    assert options.headers == {}
