from __future__ import annotations

import asyncio

from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_services import WorkerServices

MEMORY = "dev.ouro.examples.memory"


class Client:
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
        self.services = WorkerServices(self.peer)
        self.peer.handlers["handlers.invoke"] = self.services.invoke_handler

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"commands": ["remember", "search"]}

    async def lifecycle(self, params):
        return {}

    async def command(self, params):
        argument = params["call"]["argument"]
        if params["contribution_id"] == "remember":
            await self.services.call(MEMORY, "memory", "append", argument)
            return {"output": "Stored."}

        async def format_matches(matches):
            return [f"Memory: {item}" for item in matches]

        registration = await self.services.register(MEMORY, "memory", "filter", format_matches)
        try:
            matches = await self.services.call(MEMORY, "memory", "search", argument)
            return {"output": "\n".join(matches) or "No matches."}
        finally:
            await registration.close()


asyncio.run(Client().peer.serve())
