from __future__ import annotations

import json

import pytest

from ouro.config import (
    CompactionConfigManager,
    CompactionModelConfig,
)
from ouro.core.errors import ConfigError
from ouro.core.reasoning import ReasoningRequest


def test_compaction_model_config_persists_without_replacing_other_sections(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "config.json"
    path.write_text(json.dumps({"custom": {"value": "keep"}}), encoding="utf-8")
    manager = CompactionConfigManager(tmp_path)

    saved = manager.set_model(
        CompactionModelConfig(
            provider_id="glm",
            endpoint_id="summary",
            temperature=0.1,
            max_output_tokens=4096,
            reasoning=ReasoningRequest(mode="disabled", effort="high"),
        )
    )
    restored = CompactionConfigManager(tmp_path).snapshot()
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert restored == saved
    assert restored.model.provider_id == "glm"
    assert restored.model.endpoint_id == "summary"
    assert restored.model.reasoning == ReasoningRequest(mode="disabled", effort="high")
    assert raw["custom"] == {"value": "keep"}
    assert raw["compaction"]["model"]["max_output_tokens"] == 4096


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"provider_id": "glm"}, "configured together"),
        ({"reasoning": {"effort": "extreme"}}, "Input should be"),
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


def test_compaction_manager_rejects_unknown_policy_fields(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "compaction": {
                    "policy": {
                        "summary_output_tokens": 4096,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        CompactionConfigManager(tmp_path).snapshot()


def test_runtime_endpoint_changes_only_the_route(tmp_path) -> None:
    manager = CompactionConfigManager(tmp_path)
    manager.set_model(
        CompactionModelConfig(
            provider_id="glm",
            endpoint_id="summary",
            temperature=0.1,
            max_output_tokens=4096,
            reasoning=ReasoningRequest(mode="enabled", effort="high"),
        )
    )

    saved = manager.update_model(use_runtime_endpoint=True)

    assert saved.model.provider_id is None
    assert saved.model.endpoint_id is None
    assert saved.model.reasoning == ReasoningRequest(mode="enabled", effort="high")
    assert saved.model.temperature == 0.1
    assert saved.model.max_output_tokens == 4096


@pytest.mark.parametrize(
    "override",
    [
        {"temperature": 0.1},
        {"max_output_tokens": 4096},
        {"reasoning": ReasoningRequest(mode="disabled")},
    ],
)
def test_runtime_endpoint_accepts_independent_request_settings(tmp_path, override) -> None:
    manager = CompactionConfigManager(tmp_path)

    saved = manager.update_model(use_runtime_endpoint=True, **override)
    for key, value in override.items():
        assert getattr(saved.model, key) == value
