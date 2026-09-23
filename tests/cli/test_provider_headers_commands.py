from __future__ import annotations

import json

from ouro.cli.main import main
from ouro.config import ProviderConfigManager


def test_provider_headers_cli_create_preserve_clear_replace_and_redact(tmp_path, capsys):
    headers = {
        "X-Private": {"source": "literal", "value": "cli-private-value"},
        "X-Model": {"source": "context", "key": "model.id"},
    }
    common = ["--home", str(tmp_path), "--json"]
    connection = [
        "--type",
        "custom",
        "--protocol",
        "openai-chat-completions",
        "--base-url",
        "https://model.test",
    ]
    assert (
        main(["provider", "add", "gateway", *connection, "--headers", json.dumps(headers), *common])
        == 0
    )
    output = capsys.readouterr().out
    assert "cli-private-value" not in output
    added = json.loads(output)["provider"]
    assert added["headers"]["x-private"]["value"] == "***"
    provider_id = added["provider_id"]
    configs = ProviderConfigManager(tmp_path)
    provider = configs.get_provider(provider_id)
    assert provider is not None
    assert provider.headers["x-private"].value == "cli-private-value"
    assert main(["provider", "update", provider_id, "--timeout", "20", *common]) == 0
    capsys.readouterr()
    provider = configs.get_provider(provider_id)
    assert provider is not None and len(provider.headers) == 2
    assert main(["provider", "show", provider_id, *common]) == 0
    assert "cli-private-value" not in capsys.readouterr().out
    assert main(["provider", "update", provider_id, "--headers", "{}", *common]) == 0
    capsys.readouterr()
    provider = configs.get_provider(provider_id)
    assert provider is not None and provider.headers == {}
    assert (
        main(
            [
                "provider",
                "replace",
                provider_id,
                *connection,
                "--headers",
                json.dumps(headers),
                *common,
            ]
        )
        == 0
    )
    capsys.readouterr()
    provider = configs.get_provider(provider_id)
    assert provider is not None and len(provider.headers) == 2


def test_cli_header_validation_is_atomic_and_does_not_echo_secrets(tmp_path, capsys):
    common = ["--home", str(tmp_path), "--json"]
    assert (
        main(
            [
                "provider",
                "add",
                "gateway",
                "--type",
                "custom",
                "--protocol",
                "openai-chat-completions",
                *common,
            ]
        )
        == 0
    )
    added = json.loads(capsys.readouterr().out)
    provider_id = added["provider"]["provider_id"]
    path = ProviderConfigManager(tmp_path).config_path
    before = path.read_bytes()
    headers = {"X-Private": {"source": "literal", "value": "private-secret\n"}}
    assert main(["provider", "update", provider_id, "--headers", json.dumps(headers), *common]) != 0
    output = capsys.readouterr()
    assert "private-secret" not in output.out + output.err
    assert path.read_bytes() == before
