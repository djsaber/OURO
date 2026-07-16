from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from ouro.config.file import JsonConfigFile
from ouro.config.prompts import PromptConfigManager
from ouro.config.queued_inputs import QueuedInputConfigManager


def test_config_commands_preserve_concurrent_section_updates(tmp_path) -> None:
    queued_inputs = QueuedInputConfigManager(tmp_path)
    prompts = PromptConfigManager(tmp_path)
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

    def update_prompt() -> None:
        prompts.set_system_prompt("Concurrent prompt")
        second_update_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            queued_inputs.update,
            consumption_mode="all_at_once",
        )
        assert first_read_entered.wait(timeout=1)
        second = executor.submit(update_prompt)
        assert not second_update_finished.wait(timeout=0.05)
        release_first_update.set()
        first.result(timeout=2)
        second.result(timeout=2)

    raw = JsonConfigFile(tmp_path).read()
    assert raw["queued_inputs"] == {"consumption_mode": "all_at_once"}
    assert raw["prompts"] == {"system": "Concurrent prompt"}
