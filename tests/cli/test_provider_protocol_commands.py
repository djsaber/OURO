from __future__ import annotations

import json

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.cli.main import main
from ouro.config import ProviderConfigManager, ProviderRuntimeOptions


@pytest.mark.parametrize(
    ("protocol", "default_path"),
    [
        ("openai-chat-completions", "/chat/completions"),
        ("openai-responses", "/responses"),
        ("anthropic-messages", "/v1/messages"),
    ],
)
@pytest.mark.parametrize("api_path", [None, "/custom/inference"])
def test_custom_provider_creation_update_and_replacement(
    tmp_path, capsys, api_path, protocol, default_path
):
    args = [
        "provider",
        "add",
        "gateway",
        "--type",
        "custom",
        "--protocol",
        protocol,
        "--base-url",
        "https://gateway.test/v1",
        "--api-key-env",
        "GATEWAY_KEY",
        "--home",
        str(tmp_path),
        "--json",
    ]
    if api_path is not None:
        args.extend(["--api-path", api_path])
    assert main(args) == 0
    provider = json.loads(capsys.readouterr().out)["provider"]
    provider_id = provider["provider_id"]
    assert provider["display_name"] == "gateway"
    assert provider["provider_type"] == "custom"
    assert provider["protocol"] == protocol
    assert provider["api_path"] == api_path
    assert provider["endpoint_count"] == 0

    facade = ConfigurationFacade(tmp_path)
    endpoints = facade.add_endpoint(provider_id, display_name="Chat", model="custom-model")
    endpoint_id = endpoints.changed_endpoint_id
    assert endpoint_id is not None
    resolved = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(provider_id=provider_id, endpoint_id=endpoint_id)
    )
    assert resolved.protocol == protocol
    assert resolved.api_path == (api_path or default_path)
    assert (
        main(
            [
                "provider",
                "update",
                provider_id,
                "--timeout",
                "15",
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    saved = ConfigurationFacade(tmp_path).provider(provider_id)
    assert saved.protocol == protocol
    assert saved.api_path == api_path
    assert saved.endpoint_count == 1

    assert (
        main(
            [
                "provider",
                "replace",
                provider_id,
                "--type",
                "custom",
                "--protocol",
                "openai-chat-completions",
                "--base-url",
                "https://gateway.test/v1",
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    saved = ConfigurationFacade(tmp_path).provider(provider_id)
    assert saved.protocol == "openai-chat-completions"
    assert saved.api_path is None
    assert saved.endpoint_count == 0


@pytest.mark.parametrize("operation", ["add", "replace"])
def test_custom_provider_requires_explicit_protocol_without_writing(tmp_path, capsys, operation):
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(display_name="Existing", provider_type="anthropic")
    provider_id = created.changed_provider_id
    assert provider_id is not None
    before = facade.providers.config_path.read_bytes()
    assert (
        main(
            [
                "provider",
                operation,
                "New" if operation == "add" else provider_id,
                "--type",
                "custom",
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 2
    )
    assert "protocol is required" in json.loads(capsys.readouterr().out)["error"]
    assert facade.providers.config_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["add", "replace"])
@pytest.mark.parametrize("provider_type", ["anthropic", "openai"])
def test_rejected_protocol_selection_preserves_configuration(
    tmp_path,
    capsys,
    operation,
    provider_type,
):
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(display_name="Existing", provider_type="anthropic")
    provider_id = created.changed_provider_id
    assert provider_id is not None
    before = facade.providers.config_path.read_bytes()
    protocol = "openai-responses" if provider_type == "anthropic" else "openai-chat-completions"
    assert (
        main(
            [
                "provider",
                operation,
                "New" if operation == "add" else provider_id,
                "--type",
                provider_type,
                "--protocol",
                protocol,
                "--base-url",
                "https://gateway.test/v1",
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 2
    )
    assert "protocol" in json.loads(capsys.readouterr().out)["error"]
    assert facade.providers.config_path.read_bytes() == before


def test_replace_custom_provider_with_responses(tmp_path, capsys):
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(display_name="Gateway", provider_type="anthropic")
    provider_id = created.changed_provider_id
    assert provider_id is not None
    assert (
        main(
            [
                "provider",
                "replace",
                provider_id,
                "--type",
                "custom",
                "--protocol",
                "openai-responses",
                "--base-url",
                "https://gateway.test/v1",
                "--home",
                str(tmp_path),
                "--json",
            ]
        )
        == 0
    )
    provider = json.loads(capsys.readouterr().out)["provider"]
    assert provider["protocol"] == "openai-responses"
    assert provider["api_path"] is None
    assert provider["endpoint_count"] == 0
