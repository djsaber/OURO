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
    assert json.loads(output)["provider"]["headers"]["x-private"]["value"] == "***"
    configs = ProviderConfigManager(tmp_path)
    assert configs.get_provider("gateway").headers["x-private"].value == "cli-private-value"
    assert main(["provider", "update", "gateway", "--timeout", "20", *common]) == 0
    capsys.readouterr()
    assert len(configs.get_provider("gateway").headers) == 2
    assert main(["provider", "show", "gateway", *common]) == 0
    assert "cli-private-value" not in capsys.readouterr().out
    assert main(["provider", "update", "gateway", "--headers", "{}", *common]) == 0
    capsys.readouterr()
    assert configs.get_provider("gateway").headers == {}
    assert (
        main(
            [
                "provider",
                "replace",
                "gateway",
                *connection,
                "--headers",
                json.dumps(headers),
                *common,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert len(configs.get_provider("gateway").headers) == 2


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
    capsys.readouterr()
    path = ProviderConfigManager(tmp_path).config_path
    before = path.read_bytes()
    headers = {"X-Private": {"source": "literal", "value": "private-secret\n"}}
    assert main(["provider", "update", "gateway", "--headers", json.dumps(headers), *common]) != 0
    output = capsys.readouterr()
    assert "private-secret" not in output.out + output.err
    assert path.read_bytes() == before
