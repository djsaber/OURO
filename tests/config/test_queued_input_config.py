import json

import pytest

from ouro.config import QueuedInputConfigManager
from ouro.core.errors import ConfigError


def test_queued_input_config_defaults_and_persists_consumption_mode(tmp_path) -> None:
    manager = QueuedInputConfigManager(tmp_path)

    assert manager.snapshot().consumption_mode == "one_by_one"
    updated = manager.update(consumption_mode="all_at_once")

    assert updated.consumption_mode == "all_at_once"
    assert manager.snapshot() == updated
    raw = json.loads(manager.file.path.read_text(encoding="utf-8"))
    assert raw["queued_inputs"] == {"consumption_mode": "all_at_once"}


def test_queued_input_config_rejects_unknown_fields(tmp_path) -> None:
    manager = QueuedInputConfigManager(tmp_path)
    manager.file.write({"queued_inputs": {"consumption_mode": "invalid"}})

    with pytest.raises(ConfigError, match="invalid queued_inputs config"):
        manager.snapshot()
