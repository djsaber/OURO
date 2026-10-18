"""Own local process groups through spawn, parent exit and cancellation."""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import suppress
from typing import Any

from ouro.core.cancellation import settle_owned_tasks

from .windows import WindowsJob


class LocalProcess:
    def __init__(self, process: asyncio.subprocess.Process, job: WindowsJob | None) -> None:
        self.process = process
        self.job = job
        self._close_task: asyncio.Task[None] | None = None

    async def terminate(self) -> None:
        if self.job is not None:
            self.job.terminate()
        else:
            try:
                with suppress(ProcessLookupError):
                    os.kill(-self.process.pid, 9)
            except PermissionError as denied:
                # Darwin can reject a group containing only unreaped zombies.
                # Reap our leader, then still terminate any surviving descendants.
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError as timeout:
                    raise denied from timeout
                with suppress(ProcessLookupError):
                    os.kill(-self.process.pid, 9)

    async def close(self) -> None:
        if self._close_task is not None and self._close_task.done():
            if self._close_task.cancelled() or self._close_task.exception() is not None:
                self._close_task = None
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancelled = await settle_owned_tasks(self._close_task)
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        await self.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            # A stalled consumer may leave an asyncio pipe paused after all OS
            # processes have exited. Close only the transports owned here.
            close_process_transports(self.process)
            await self.process.wait()
        if self.job is not None:
            self.job.close()


async def start_process(*arguments: str, shell: bool = False, **options: Any) -> LocalProcess:
    task = asyncio.create_task(_start_process(arguments, shell=shell, options=options))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await settle_owned_tasks(task)
        owner = task.result()
        await owner.close()
        raise


async def _start_process(
    arguments: tuple[str, ...], *, shell: bool, options: dict[str, Any]
) -> LocalProcess:
    job = WindowsJob() if os.name == "nt" else None
    process = None
    try:
        if job is not None:
            options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | 0x0004
            )  # CREATE_SUSPENDED: attach the job before any plugin code runs.
        else:
            options["start_new_session"] = True
        if shell:
            process = await asyncio.create_subprocess_shell(arguments[0], **options)
        else:
            process = await asyncio.create_subprocess_exec(*arguments, **options)
        if job is not None:
            job.attach_and_resume(process.pid)
        return LocalProcess(process, job)
    except BaseException:
        if process is not None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if job is not None:
            job.close()
        raise


def close_process_transports(process: asyncio.subprocess.Process) -> None:
    transport = getattr(process, "_transport", None)
    if transport is not None:
        transport.close()
