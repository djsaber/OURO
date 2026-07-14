from __future__ import annotations

import json

from ouro.cli.main import main
from ouro.config import ProviderConfigManager


def test_provider_templates_command_lists_builtin_catalog(capsys) -> None:
    code = main(["provider", "templates", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert {item["template_id"] for item in payload["templates"]} >= {
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "qwen",
        "glm",
        "openrouter",
        "mistral",
    }


def test_provider_configure_command_persists_complete_default_route(
    tmp_path,
    capsys,
) -> None:
    code = main(
        [
            "provider",
            "configure",
            "anthropic",
            "--provider-id",
            "claude",
            "--model",
            "claude-custom",
            "--api-key-env",
            "CLAUDE_KEY",
            "--default",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"]["provider_id"] == "claude"
    assert payload["provider"]["kind"] == "anthropic-compatible"
    assert payload["provider"]["default_endpoint"] == "claude-sonnet-5"
    assert payload["provider"]["endpoints"][0]["model_id"] == "claude-custom"
    snapshot = ProviderConfigManager(tmp_path).snapshot()
    assert snapshot.default_provider_id == "claude"
    provider = next(item for item in snapshot.providers if item.provider_id == "claude")
    assert provider.endpoint() is not None
    assert provider.credential_ref.display() == "env:CLAUDE_KEY"


def test_unknown_provider_template_returns_config_error(tmp_path, capsys) -> None:
    code = main(
        [
            "provider",
            "configure",
            "unknown",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )

    assert code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ConfigError"
    assert not (tmp_path / "config" / "config.json").exists()
