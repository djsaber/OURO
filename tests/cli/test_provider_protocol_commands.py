from __future__ import annotations

import json

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.cli.main import main
from ouro.config import ProviderConfigManager, ProviderRuntimeOptions


@pytest.mark.parametrize("api_path", [None, "/custom/responses"])
def test_custom_responses_provider_creation_update_and_replacement(tmp_path, capsys, api_path):
    args = [
        "provider",
        "add",
        "gateway",
        "--type",
        "openai-compatible",
        "--protocol",
        "openai-responses",
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
    assert provider["protocol"] == "openai-responses"
    assert provider["api_path"] == api_path
    assert provider["endpoint_count"] == 0

    facade = ConfigurationFacade(tmp_path)
    facade.save_endpoint("gateway", "chat", model="custom-model")
    resolved = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="gateway", endpoint_id="chat")
    )
    assert resolved.protocol == "openai-responses"
    assert resolved.api_path == (api_path or "/responses")
    assert (
        main(
            [
                "provider",
                "update",
                "gateway",
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
    saved = ConfigurationFacade(tmp_path).provider("gateway")
    assert saved.protocol == "openai-responses"
    assert saved.api_path == api_path
    assert saved.endpoint_count == 1

    assert (
        main(
            [
                "provider",
                "replace",
                "gateway",
                "--type",
                "openai-compatible",
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
    saved = ConfigurationFacade(tmp_path).provider("gateway")
    assert saved.protocol == "openai-chat-completions"
    assert saved.api_path is None
    assert saved.endpoint_count == 0


@pytest.mark.parametrize("operation", ["add", "replace"])
@pytest.mark.parametrize("provider_type", ["anthropic-compatible", "openai"])
def test_rejected_protocol_selection_preserves_configuration(
    tmp_path,
    capsys,
    operation,
    provider_type,
):
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("existing", provider_type="anthropic")
    before = facade.config_path.read_bytes()
    protocol = (
        "openai-responses" if provider_type == "anthropic-compatible" else "openai-chat-completions"
    )
    assert (
        main(
            [
                "provider",
                operation,
                "new" if operation == "add" else "existing",
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
    assert facade.config_path.read_bytes() == before


def test_replace_custom_provider_with_responses(tmp_path, capsys):
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider("gateway", provider_type="anthropic")
    assert (
        main(
            [
                "provider",
                "replace",
                "gateway",
                "--type",
                "openai-compatible",
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
