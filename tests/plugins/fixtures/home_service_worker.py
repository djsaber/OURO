from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ouro.sdk.plugins.worker import WorkerPeer


class ServiceWorker:
    def __init__(self):
        self.data = None
        self.task = None
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "background.start": self.start,
                "background.stop": self.stop,
            }
        )

    async def initialize(self, params):
        self.data = Path(params["data_directory"])
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"background_services": ["singleton"]}

    async def lifecycle(self, params):
        return {}

    def record(self, action):
        with (self.data / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"action": action, "pid": os.getpid()}) + "\n")

    async def start(self, params):
        assert params["contribution_id"] == "singleton"
        if self.task is None:
            # An exclusive file represents a resource with exactly one live owner.
            with (self.data / "owner").open("x", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            self.task = asyncio.create_task(asyncio.Event().wait())
            self.record("start")
        return {}

    async def stop(self, params):
        assert params["contribution_id"] == "singleton"
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
            (self.data / "owner").unlink()
            self.record("stop")
        return {}


asyncio.run(ServiceWorker().peer.serve())
