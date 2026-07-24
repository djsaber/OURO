from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

InputFunc = Callable[[str], str]
PromptFunc = Callable[[str], None]


class InteractiveInput:
    """Own one blocking terminal line read while allowing the Host to replace its prompt."""

    def __init__(self, input_func: InputFunc, prompt_func: PromptFunc) -> None:
        self._input_func = input_func
        self._prompt_func = prompt_func
        self._active: asyncio.Task[str] | None = None

    def begin(self, prompt: str) -> asyncio.Task[str]:
        if self._active is not None:
            raise RuntimeError("interactive input already has an active line read")
        self._prompt_func(prompt)
        task = asyncio.create_task(
            asyncio.to_thread(self._input_func, ""),
            name="ouro-terminal-line",
        )
        self._active = task
        return task

    async def finish(self, task: asyncio.Task[str]) -> str:
        if task is not self._active:
            raise RuntimeError("interactive input task does not own the active line read")
        try:
            return await task
        finally:
            self._active = None

    async def read(self, prompt: str) -> str:
        return await self.finish(self.begin(prompt))

    def break_prompt(self) -> None:
        if self._active is None:
            raise RuntimeError("interactive input has no active prompt")
        self._prompt_func("\n")

    def replace_prompt(self, prompt: str) -> None:
        if self._active is None:
            raise RuntimeError("interactive input has no active prompt")
        self._prompt_func(prompt)


def write_terminal_prompt(prompt: str) -> None:
    sys.stdout.write(prompt)
    sys.stdout.flush()


__all__ = ["InputFunc", "InteractiveInput", "PromptFunc", "write_terminal_prompt"]
