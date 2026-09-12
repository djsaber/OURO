from __future__ import annotations

import json

import pytest

from ouro.cli.main import main
from ouro.cli.parser import build_parser
from ouro.config import ProviderConfigManager
from ouro.config.provider_templates import get_provider_template


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
        "kimi",
        "glm",
        "grok",
        "openrouter",
        "mistral",
    }
    kimi = next(item for item in payload["templates"] if item["template_id"] == "kimi")
    assert kimi["default_endpoint"] == "kimi-k3"
    assert kimi["endpoint_count"] == 4


def test_provider_templates_command_reports_template_file_notices(
    tmp_path,
    capsys,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "provider-templates.json").write_text("{ broken", encoding="utf-8")

    code = main(["provider", "templates", "--home", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert {item["template_id"] for item in payload["templates"]} >= {"openai"}
    assert any("failed to load" in notice for notice in payload["notices"])


def test_restore_template_command_drops_local_modifications(tmp_path, capsys) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    assert main(["provider", "templates", "--home", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    path = config_dir / "provider-templates.json"
    root = json.loads(path.read_text(encoding="utf-8"))
    root["templates"]["glm"]["display_name"] = "My GLM"
    path.write_text(json.dumps(root), encoding="utf-8")

    code = main(["provider", "restore-template", "glm", "--home", str(tmp_path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["template"]["display_name"] == "Zhipu GLM"
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored["templates"]["glm"]["display_name"] == "Zhipu GLM"


def test_provider_add_integrated_type_persists_complete_default_route(
    tmp_path,
    capsys,
) -> None:
    code = main(
        [
            "provider",
            "add",
            "claude",
            "--type",
            "anthropic",
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
    provider = payload["provider"]
    assert provider["provider_id"] == "claude"
    assert provider["provider_type"] == "anthropic"
    assert provider["protocol"] == "anthropic-messages"
    assert provider["default_endpoint"] == "claude-sonnet-5"
    assert provider["endpoint_count"] == 3
    assert next(item for item in provider["endpoints"] if item["default"])["model_id"] == (
        "claude-custom"
    )
    snapshot = ProviderConfigManager(tmp_path).snapshot()
    assert snapshot.default_provider_id == "claude"
    configured = ProviderConfigManager(tmp_path).get_provider("claude")
    assert configured is not None
    assert configured.credential_ref.display() == "env:CLAUDE_KEY"


def test_provider_add_grok_installs_latest_reviewed_endpoint(tmp_path, capsys) -> None:
    code = main(
        [
            "provider",
            "add",
            "xai",
            "--type",
            "grok",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    provider = payload["provider"]
    assert provider["provider_id"] == "xai"
    assert provider["provider_type"] == "grok"
    assert provider["protocol"] == "openai-chat-completions"
    assert provider["default_endpoint"] == "grok-4.6"
    assert provider["endpoint_count"] == 1
    configured = ProviderConfigManager(tmp_path).get_provider("xai")
    assert configured is not None
    assert configured.base_url == "https://api.x.ai/v1"
    assert configured.api_path == "/chat/completions"
    assert configured.credential_ref.display() == "env:XAI_API_KEY"


def test_provider_update_json_exposes_safe_template_merge_report(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "provider",
                "add",
                "primary",
                "--type",
                "openai",
                "--home",
                str(tmp_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = main(
        [
            "provider",
            "update",
            "primary",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    revision = get_provider_template("openai").revision
    assert payload["operation"] == "update"
    assert payload["provider"]["template"] == {
        "template_id": "openai",
        "origin": "inline",
        "revision": revision,
        "tracked": True,
    }
    assert payload["template_update"] == {
        "template_id": "openai",
        "from_revision": revision,
        "to_revision": revision,
        "adopted": False,
        "changed": False,
        "added_endpoints": [],
        "updated_endpoints": [],
        "removed_endpoints": [],
        "preserved_endpoints": [],
        "conflicted_endpoints": [],
        "updated_fields": [],
        "preserved_fields": [],
    }


def test_removed_configure_custom_and_protocol_are_not_parseable() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["provider", "configure", "openai"])
    with pytest.raises(SystemExit):
        parser.parse_args(["provider", "add", "local", "--type", "custom"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "provider",
                "add",
                "local",
                "--type",
                "openai-compatible",
                "--protocol",
                "openai-chat-completions",
            ]
        )


def test_integrated_type_conflict_prompts_update_or_replace(tmp_path, capsys) -> None:
    assert (
        main(
            [
                "provider",
                "add",
                "primary",
                "--type",
                "anthropic",
                "--home",
                str(tmp_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = main(
        [
            "provider",
            "add",
            "primary",
            "--type",
            "openai",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    error = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "provider update" in error["error"]
    assert "provider replace" in error["error"]
    assert ProviderConfigManager(tmp_path).get_provider("primary").provider_type == "anthropic"

    code = main(
        [
            "provider",
            "replace",
            "primary",
            "--type",
            "openai",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["operation"] == "replace"
    assert payload["provider"]["provider_type"] == "openai"
    assert payload["provider"]["endpoint_count"] == 3


def test_compatible_add_update_and_replace_infer_protocol(tmp_path, capsys) -> None:
    code = main(
        [
            "provider",
            "add",
            "local",
            "--type",
            "anthropic-compatible",
            "--base-url",
            "https://example.test",
            "--api-key-env",
            "LOCAL_KEY",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["provider"]["provider_type"] == "anthropic-compatible"
    assert payload["provider"]["protocol"] == "anthropic-messages"
    assert payload["provider"]["endpoint_count"] == 0

    code = main(
        [
            "provider",
            "update",
            "local",
            "--timeout",
            "12",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["provider"]["provider_type"] == "anthropic-compatible"
    assert payload["provider"]["protocol"] == "anthropic-messages"
    assert payload["provider"]["timeout"] == 12

    code = main(
        [
            "provider",
            "replace",
            "local",
            "--type",
            "openai-compatible",
            "--base-url",
            "https://replacement.test/v1",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["provider"]["provider_type"] == "openai-compatible"
    assert payload["provider"]["protocol"] == "openai-chat-completions"
    assert payload["provider"]["endpoint_count"] == 0


def test_compatible_add_rejects_template_only_route_options(tmp_path, capsys) -> None:
    code = main(
        [
            "provider",
            "add",
            "local",
            "--type",
            "openai-compatible",
            "--model",
            "model-name",
            "--home",
            str(tmp_path),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "explicit endpoint commands" in payload["error"]
    assert not (tmp_path / "config" / "config.json").exists()
