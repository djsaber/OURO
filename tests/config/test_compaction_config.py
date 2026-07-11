from __future__ import annotations

import json

import pytest

from ouro.config import (
    CompactionConfigManager,
    CompactionModelConfig,
)
from ouro.core.errors import ConfigError


def test_compaction_model_config_persists_without_replacing_other_sections(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "config.json"
    path.write_text(json.dumps({"providers": {"default": "faux"}}), encoding="utf-8")
    manager = CompactionConfigManager(tmp_path)

    saved = manager.set_model(
        CompactionModelConfig(
            provider_id="glm",
            endpoint_id="summary",
            temperature=0.1,
            max_output_tokens=4096,
            thinking_enabled=False,
            thinking_level="off",
        )
    )
    restored = CompactionConfigManager(tmp_path).snapshot()
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert restored == saved
    assert restored.model.provider_id == "glm"
    assert restored.model.endpoint_id == "summary"
    assert restored.model.thinking_level == "off"
    assert raw["providers"] == {"default": "faux"}
    assert raw["compaction"]["model"]["max_output_tokens"] == 4096


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"provider_id": "glm"}, "configured together"),
        (
            {"thinking_enabled": False, "thinking_level": "high"},
            "must be off",
        ),
        (
            {"thinking_enabled": True, "thinking_level": "off"},
            "cannot be off",
        ),
    ],
)
def test_compaction_model_config_rejects_inconsistent_options(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        CompactionModelConfig.model_validate(kwargs)


def test_compaction_manager_rejects_invalid_persisted_shape(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "compaction": {
                    "model": {
                        "provider_id": "glm",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="configured together"):
        CompactionConfigManager(tmp_path).snapshot()
