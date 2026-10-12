"""Public SDK service package shared by in-process and worker lifetime tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from ouro.sdk.plugins import v3 as sdk
from ouro.sdk.plugins.services import ServiceCall, ServiceContribution
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_services import WorkerServices

LEAF = "test.lifetime.leaf"
BRANCH = "test.lifetime.branch"
MEMORY = "test.lifetime.memory"
METHODS = ("identity", "read_leaf", "read", "install", "search", "filter")


class Plugin:
    def __init__(self, services):
        self.services = services
        self.token = uuid4().hex
        self.filters = []

    async def identity(self, call):
        return self.token

    async def read_leaf(self, call):
        return await self.services.call(LEAF, "api", "identity")

    async def blocked(self, arguments):
        Path(arguments["ready"]).touch()
        while not Path(arguments["release"]).exists():
            await asyncio.sleep(0.01)
        result = [
            await self.services.call(LEAF, "api", "identity"),
            await self.services.call(BRANCH, "api", "read_leaf"),
        ]
        if arguments["fail"]:
            raise ValueError("service failed after reading dependencies")
        return result

    async def read(self, call):
        return await self.blocked(call.arguments)

    async def install(self, call):
        await self.services.register(MEMORY, "api", "filter", self.blocked)

    async def search(self, call):
        value = call.arguments
        for callback in tuple(self.filters):
            value = await callback(value)
        return value

    async def filter(self, call):
        self.filters.append(call.handler)
        call.on_close(lambda: self.filters.remove(call.handler))

    async def prepare(self):
        return sdk.ContributionSet(
            services=[ServiceContribution("api", {name: getattr(self, name) for name in METHODS})]
        )

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def create_plugin(context):
    return Plugin(context.services)


class Worker:
    def __init__(self):
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "services.call": self.call,
                "services.register": self.register,
                "registrations.close": self.unregister,
            }
        )
        self.services = WorkerServices(self.peer)
        self.peer.handlers["handlers.invoke"] = self.services.invoke_handler
        self.plugin = Plugin(self.services)
        self.removals = {}

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"services": ["api"]}

    async def lifecycle(self, params):
        return {}

    async def call(self, params):
        return await getattr(self.plugin, params["method"])(
            ServiceCall(params["caller_id"], params.get("arguments"))
        )

    async def register(self, params):
        async def callback(arguments):
            return await self.peer.request(
                "handlers.invoke", {"handler_id": params["handler_id"], "arguments": arguments}
            )

        await self.plugin.filter(
            ServiceCall(
                params["caller_id"],
                params.get("arguments"),
                callback,
                lambda cleanup: self.removals.__setitem__(params["registration_id"], cleanup),
            )
        )

    async def unregister(self, params):
        self.removals.pop(params["registration_id"])()


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
