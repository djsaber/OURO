from __future__ import annotations

import json

from ouro.application.configuration import ConfigurationFacade
from ouro.cli.main import main
from ouro.config import CompactionConfigManager


def test_config_compaction_command_persists_and_displays_model_options(
    tmp_path,
    capsys,
) -> None:
    code = main(
        [
            "config",
            "compaction",
            "--home",
            str(tmp_path),
            "--provider",
            "glm",
            "--endpoint",
            "summary",
            "--temperature",
            "0.1",
            "--max-output-tokens",
            "4096",
            "--thinking-mode",
            "disabled",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    restored = CompactionConfigManager(tmp_path).snapshot()

    assert code == 0
    assert payload["status"] == "ok"
    assert payload["compaction"]["model"]["endpoint_id"] == "summary"
    assert restored.model.provider_id == "glm"
    assert restored.model.temperature == 0.1
    assert restored.model.reasoning is not None
    assert restored.model.reasoning.mode == "disabled"


def test_config_compaction_runtime_endpoint_preserves_request_settings(
    tmp_path,
    capsys,
) -> None:
    assert (
        main(
            [
                "config",
                "compaction",
                "--home",
                str(tmp_path),
                "--provider",
                "glm",
                "--endpoint",
                "summary",
                "--temperature",
                "0.2",
                "--thinking-mode",
                "enabled",
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = main(
        [
            "config",
            "compaction",
            "--home",
            str(tmp_path),
            "--runtime-endpoint",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    model = CompactionConfigManager(tmp_path).snapshot().model

    assert code == 0
    assert model.provider_id is None
    assert model.endpoint_id is None
    assert model.temperature == 0.2
    assert model.reasoning.mode == "enabled"
    assert "provider_id" not in payload["compaction"]["model"]
    assert payload["compaction"]["model"]["reasoning"]["mode"] == "enabled"


def test_config_compaction_accepts_output_limit_for_runtime_route(
    tmp_path,
    capsys,
) -> None:
    code = main(
        [
            "config",
            "compaction",
            "--home",
            str(tmp_path),
            "--max-output-tokens",
            "4096",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert not captured.err


def test_config_compaction_rejects_partial_endpoint(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "compaction",
            "--home",
            str(tmp_path),
            "--provider",
            "glm",
        ]
    )
    captured = capsys.readouterr()

    assert code != 0
    assert "configured together" in captured.err


def test_config_compaction_accepts_reasoning_for_runtime_route(tmp_path, capsys) -> None:
    code = main(
        [
            "config",
            "compaction",
            "--home",
            str(tmp_path),
            "--thinking-mode",
            "enabled",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert not captured.err


def test_config_compaction_displays_default_reasoning(tmp_path, capsys) -> None:
    code = main(["config", "compaction", "--home", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "Max output tokens: unlimited" in captured.out
    assert "Thinking mode: Endpoint default" in captured.out
    assert "Thinking effort: Endpoint default" in captured.out


def test_config_compaction_default_model_requires_a_selected_system_endpoint(
    tmp_path, capsys
) -> None:
    code = main(["config", "compaction", "--home", str(tmp_path), "--default-model"])
    assert code != 0
    assert "system default endpoint is not configured" in capsys.readouterr().err

    ConfigurationFacade(tmp_path).add_provider(
        display_name="Primary", provider_type="openai", make_default=True
    )
    code = main(["config", "compaction", "--home", str(tmp_path), "--default-model"])
    assert code == 0
    assert "system default endpoint" in capsys.readouterr().out
    model = CompactionConfigManager(tmp_path).snapshot().model
    assert model.use_default_model is True
    assert model.has_dedicated_endpoint is False
