from __future__ import annotations

import asyncio
import sys

from ouro.sdk.plugins.processes import ProcessRequest
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_processes import WorkerProcesses


class Execution:
    def __init__(self):
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "command.call": self.command,
            },
            max_requests=1,
        )
        self.processes = WorkerProcesses(self.peer)
        self.peer.handlers["processes.output"] = self.processes.output

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"commands": ["python"]}

    async def lifecycle(self, params):
        return {}

    async def command(self, params):
        last_sequence = 0

        async def output(chunk):
            nonlocal last_sequence
            last_sequence = chunk.sequence

        call = params["call"]
        result = await self.processes.run(
            ProcessRequest((sys.executable, "-c", call["argument"]), cwd=call["workspace"]),
            on_output=output,
        )
        return {
            "output": result.stdout + result.stderr,
            "data": {"returncode": result.returncode, "output_chunks": last_sequence},
        }


asyncio.run(Execution().peer.serve())
