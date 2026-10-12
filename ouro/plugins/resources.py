from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from ouro.plugins.invocation import current_invocation
from ouro.plugins.paths import portable_relative_path


class ScopedPluginDataStore:
    """Plugin-owned files, writable only after the generation is published."""

    def __init__(self, root: Path, *, read_only: bool = False) -> None:
        self.root = root
        self._read_only = read_only

    def read_bytes(self, relative_path: str) -> bytes:
        return self._path(relative_path).read_bytes()

    def write_bytes(self, relative_path: str, value: bytes) -> None:
        self._require_writable()
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, relative_path: str) -> None:
        self._require_writable()
        self._path(relative_path).unlink(missing_ok=True)

    def activate(self) -> None:
        self._read_only = False

    def close(self) -> None:
        self._read_only = True

    def _require_writable(self) -> None:
        if self._read_only:
            raise PermissionError("plugin data is read-only outside an active generation")
        invocation = current_invocation.get()
        if invocation is not None:
            invocation.require_active()
            if not invocation.writable:
                raise PermissionError("plugin data is read-only during this invocation")

    def _path(self, value: str) -> Path:
        relative = portable_relative_path(value)
        path = self.root.joinpath(*relative.parts).resolve(strict=False)
        if not path.is_relative_to(self.root.resolve(strict=False)):
            raise ValueError("plugin data path escapes private data root")
        return path


class OwnedTaskGroup:
    """Own background work from registration through activation and settlement."""

    def __init__(
        self,
        on_failure: Callable[[Exception], None] | None = None,
        *,
        active: bool = True,
    ) -> None:
        self._tasks: dict[asyncio.Task[object], Awaitable[object]] = {}
        self._closed = False
        self._on_failure = on_failure
        self._activated = asyncio.Event()
        if active:
            self._activated.set()

    def start(self, coroutine: Awaitable[object], *, name: str | None = None) -> None:
        if self._closed:
            self._discard(coroutine)
            raise RuntimeError("plugin task group is closed")

        async def run() -> object:
            await self._activated.wait()
            try:
                return await coroutine
            except Exception as exc:
                if self._on_failure is not None:
                    self._on_failure(exc)
                raise

        task = asyncio.create_task(run(), name=name)
        self._tasks[task] = coroutine
        task.add_done_callback(self._task_finished)

    def activate(self) -> None:
        if self._closed:
            raise RuntimeError("plugin task group is closed")
        self._activated.set()

    def _task_finished(self, task: asyncio.Task[object]) -> None:
        self._discard(self._tasks.pop(task))
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _discard(coroutine: Awaitable[object]) -> None:
        if inspect.iscoroutine(coroutine):
            coroutine.close()
        elif isinstance(coroutine, asyncio.Future):
            coroutine.cancel()

    async def close(self, *, timeout: float = 5.0) -> None:
        tasks = tuple(self._tasks)
        if not self._closed:
            self._closed = True
            for task in tasks:
                task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                raise TimeoutError(f"{len(pending)} plugin tasks have not stopped")
