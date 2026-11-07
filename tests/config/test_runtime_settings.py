from __future__ import annotations

import pytest

from ouro.config.compaction import CompactionConfig, CompactionConfigManager
from ouro.config.file import JsonConfigFile
from ouro.config.runtime import RuntimeSettingsResolver
from ouro.core.compaction import CompactionPolicy
from ouro.core.errors import ConfigError


def test_settings_reuse_unchanged_file_and_reload_external_updates(tmp_path, monkeypatch):
    resolver = RuntimeSettingsResolver(tmp_path)
    reads = []
    read = resolver.file.read

    def counted_read():
        reads.append(True)
        return read()

    monkeypatch.setattr(resolver.file, "read", counted_read)
    initial = resolver.snapshot()
    assert resolver.snapshot() is initial
    assert len(reads) == 1
    manager = CompactionConfigManager(tmp_path)
    manager.save(CompactionConfig(policy=CompactionPolicy(trigger_ratio=0.6)))
    updated = resolver.snapshot()
    assert updated.compaction.policy.trigger_ratio == 0.6
    assert initial.compaction.policy.trigger_ratio == 0.8
    assert resolver.snapshot() is updated
    assert len(reads) == 2
    resolver.file.path.unlink()
    assert resolver.snapshot().compaction.policy.trigger_ratio == 0.8


def test_invalid_update_never_reuses_old_settings_and_can_be_corrected(tmp_path):
    resolver = RuntimeSettingsResolver(tmp_path)
    resolver.snapshot()
    config = JsonConfigFile(tmp_path)
    config.write({"compaction": {"policy": {"trigger_ratio": 0.1, "target_ratio": 0.2}}})
    for _ in range(2):
        with pytest.raises(ConfigError, match="target_ratio"):
            resolver.snapshot()
    config.write({"compaction": {"policy": {"trigger_ratio": 0.7}}})
    assert resolver.snapshot().compaction.policy.trigger_ratio == 0.7


def test_optional_sections_keep_existing_null_semantics(tmp_path):
    JsonConfigFile(tmp_path).write(
        {"permissions": None, "tool_execution": None, "queued_inputs": None, "skills": {}}
    )
    settings = RuntimeSettingsResolver(tmp_path).snapshot()
    assert settings.permissions.tools == {}
    assert settings.queued_inputs.consumption_mode == "one_by_one"


def test_unrelated_config_edit_reuses_runtime_settings(tmp_path):
    resolver = RuntimeSettingsResolver(tmp_path)
    initial = resolver.snapshot()
    JsonConfigFile(tmp_path).write({"skills": {"disabled": ["unused"]}})
    assert resolver.snapshot() is initial
