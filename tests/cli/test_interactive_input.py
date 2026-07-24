from __future__ import annotations

import threading

import pytest

from ouro.cli.interactive_input import InteractiveInput


async def test_interactive_input_replaces_prompt_without_starting_second_reader() -> None:
    release = threading.Event()
    prompts: list[str] = []
    reader_prompts: list[str] = []

    def read_line(prompt: str) -> str:
        reader_prompts.append(prompt)
        assert release.wait(timeout=5)
        return "answer"

    terminal = InteractiveInput(read_line, prompts.append)
    task = terminal.begin("ouro> ")
    terminal.break_prompt()
    terminal.replace_prompt("Answer subagent: ")

    with pytest.raises(RuntimeError, match="active line read"):
        terminal.begin("second> ")

    release.set()
    assert await terminal.finish(task) == "answer"
    assert prompts == ["ouro> ", "\n", "Answer subagent: "]
    assert reader_prompts == [""]


async def test_interactive_input_allows_next_read_after_previous_line_finishes() -> None:
    responses = iter(["first", "second"])
    prompts: list[str] = []
    terminal = InteractiveInput(lambda _prompt: next(responses), prompts.append)

    assert await terminal.read("one> ") == "first"
    assert await terminal.read("two> ") == "second"
    assert prompts == ["one> ", "two> "]
