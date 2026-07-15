from __future__ import annotations

import json

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


def test_config_compaction_runtime_endpoint_preserves_parameter_overrides(
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
    assert "provider_id" not in payload["compaction"]["model"]


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
