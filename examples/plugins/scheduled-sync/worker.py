from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime
from pathlib import Path

from scheduled_sync_plugin import ScheduledSync

from ouro.sdk.plugins import v4 as sdk
from ouro.sdk.plugins.scheduled import ScheduledInvocation
from ouro.sdk.plugins.services import ServiceCall
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_scheduling import WorkerScheduler


class LocalData:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read_bytes(self, relative_path: str) -> bytes:
        return (self.root / relative_path).read_bytes()

    def write_bytes(self, relative_path: str, value: bytes) -> None:
        stream = tempfile.NamedTemporaryFile(dir=self.root, delete=False)
        temporary = Path(stream.name)
        try:
            with stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / relative_path)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, relative_path: str) -> None:
        (self.root / relative_path).unlink(missing_ok=True)


class Worker:
    def __init__(self) -> None:
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "command.call": self.command,
                "services.call": self.call,
            },
            max_requests=1,
        )

    async def initialize(self, params):
        if "scheduler" not in params["features"]:
            raise RuntimeError("This Host does not provide persistent scheduling")
        self.plugin = ScheduledSync(
            WorkerScheduler(self.peer), LocalData(Path(params["data_directory"]))
        )
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        await self.plugin.prepare()
        return {"commands": ["sync-plan"], "services": ["sync"]}

    async def lifecycle(self, params):
        return {}

    async def command(self, params):
        call = params["call"]
        result = await self.plugin.command(sdk.CommandCall(call["command_id"], call["argument"]))
        return {"output": result.output}

    async def call(self, params):
        scheduled = params.get("scheduled")
        invocation = (
            ScheduledInvocation(
                scheduled["task_id"],
                scheduled["execution_id"],
                datetime.fromisoformat(scheduled["scheduled_at"]),
            )
            if scheduled is not None
            else None
        )
        task = asyncio.create_task(
            asyncio.to_thread(
                self.plugin.sync,
                ServiceCall(params["caller_id"], params.get("arguments"), scheduled=invocation),
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Settle the owned file write before acknowledging callback cancellation.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
