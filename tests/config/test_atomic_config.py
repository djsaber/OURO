from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouro.config.file import JsonConfigFile
from ouro.config.permissions import PermissionConfigManager
from ouro.config.queued_inputs import QueuedInputConfigManager
from ouro.core.errors import ConfigError


def test_config_write_retries_sharing_conflict_and_preserves_data_on_sync_failure(
    tmp_path, monkeypatch
):
    from pathlib import Path

    import ouro.local_io as local_io

    config = JsonConfigFile(tmp_path)
    config.write({"original": True})
    replace = Path.replace
    attempts = []

    def transient_replace(source, target):
        attempts.append(True)
        if len(attempts) == 1:
            error = PermissionError("sharing conflict")
            error.winerror = 32
            raise error
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", transient_replace)
    monkeypatch.setattr(local_io, "sleep", lambda _: None)
    config.write({"replacement": True})
    assert config.read() == {"replacement": True}
    assert len(attempts) == 2

    def fail_sync(_fd):
        raise OSError("injected sync failure")

    monkeypatch.setattr(local_io.os, "fsync", fail_sync)
    with pytest.raises(ConfigError, match="could not write config"):
        config.write({"discarded": True})
    assert config.read() == {"replacement": True}
    assert list(config.path.parent.iterdir()) == [config.path]


def test_config_serialization_failure_preserves_existing_file(tmp_path):
    config = JsonConfigFile(tmp_path)
    config.write({"original": True})
    with pytest.raises(ConfigError, match="could not serialize config"):
        config.write({"invalid": object()})
    assert config.read() == {"original": True}
    assert list(config.path.parent.iterdir()) == [config.path]


def test_config_commands_preserve_concurrent_section_updates(tmp_path) -> None:
    queued_inputs = QueuedInputConfigManager(tmp_path)
    permissions = PermissionConfigManager(tmp_path)
    first_read_entered = threading.Event()
    release_first_update = threading.Event()
    second_update_finished = threading.Event()
    original_read = queued_inputs.file.read
    read_count = 0

    def blocking_read():
        nonlocal read_count
        raw = original_read()
        read_count += 1
        if read_count == 1:
            first_read_entered.set()
            assert release_first_update.wait(timeout=2)
        return raw

    queued_inputs.file.read = blocking_read  # type: ignore[method-assign]

    def update_permission() -> None:
        permissions.set_tool_mode("Read", "auto")
        second_update_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            queued_inputs.update,
            consumption_mode="all_at_once",
        )
        assert first_read_entered.wait(timeout=1)
        second = executor.submit(update_permission)
        assert not second_update_finished.wait(timeout=0.05)
        release_first_update.set()
        first.result(timeout=2)
        second.result(timeout=2)

    raw = JsonConfigFile(tmp_path).read()
    assert raw["queued_inputs"] == {"consumption_mode": "all_at_once"}
    assert permissions.snapshot().tools == {"Read": "auto"}


def test_permission_config_preserves_tool_and_plugin_overrides_independently(tmp_path) -> None:
    permissions = PermissionConfigManager(tmp_path)

    permissions.set_plugin_mode("dev.ouro.deploy", "approval")
    permissions.set_tool_mode("deploy__preview", "auto")
    permissions.clear_plugin_mode("dev.ouro.deploy")

    snapshot = permissions.snapshot()
    assert snapshot.plugins == {}
    assert snapshot.tools == {"deploy__preview": "auto"}

    permissions.set_plugin_mode("dev.ouro.deploy", "deny")
    permissions.clear_tool_mode("deploy__preview")
    snapshot = permissions.snapshot()
    assert snapshot.plugins == {"dev.ouro.deploy": "deny"}
    assert snapshot.tools == {}


def test_config_lock_timeout_preserves_data_and_allows_later_update(tmp_path) -> None:
    config = JsonConfigFile(tmp_path)
    config.write({"existing": True})

    def update() -> None:
        with config.mutation(timeout=0.03):
            config.write({"late": True})

    with ThreadPoolExecutor(max_workers=1) as executor:
        with config.mutation():
            future = executor.submit(update)
            with pytest.raises(ConfigError, match="timed out"):
                future.result(timeout=2)
    assert config.read() == {"existing": True}
    config.write({"next": True})
    assert config.read() == {"next": True}


def test_cancelled_config_waiter_never_runs_transaction(tmp_path) -> None:
    config = JsonConfigFile(tmp_path)
    config.write({"existing": True})
    cancelled = threading.Event()
    waiting = threading.Event()

    def update() -> None:
        waiting.set()
        with config.mutation(cancel_event=cancelled):
            config.write({"late": True})

    with ThreadPoolExecutor(max_workers=1) as executor:
        with config.mutation():
            future = executor.submit(update)
            assert waiting.wait(5)
            cancelled.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=2)
    assert config.read() == {"existing": True}
    config.write({"next": True})
    assert config.read() == {"next": True}


def test_started_config_transaction_finishes_despite_acquisition_cancellation(tmp_path) -> None:
    config = JsonConfigFile(tmp_path)
    cancelled = threading.Event()
    with config.mutation(cancel_event=cancelled):
        cancelled.set()
        config.write({"committed": True})
    assert config.read() == {"committed": True}


def test_failed_transaction_releases_config_lock_without_reclassifying_error(tmp_path) -> None:
    config = JsonConfigFile(tmp_path)
    with pytest.raises(TimeoutError, match="operation timeout"):
        with config.mutation():
            raise TimeoutError("operation timeout")

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(config.write, {"next": True}).result(timeout=2)
    assert config.read() == {"next": True}
