import asyncio
from types import SimpleNamespace

from driver_dependency_plugin import Plugin

from ouro.sdk.plugins.worker import WorkerPeer


class Worker:
    def __init__(self):
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "background.start": self.start,
                "background.stop": self.stop,
                "services.call": self.call,
            }
        )

    async def initialize(self, params):
        self.plugin = Plugin(SimpleNamespace(configuration=params["configuration"]))
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"services": ["value"], "background_services": ["watcher"]}

    async def lifecycle(self, params):
        return {}

    async def start(self, params):
        await self.plugin.start_background()
        return {}

    async def stop(self, params):
        await self.plugin.stop_background()
        return {}

    async def call(self, params):
        return await self.plugin.read(None)


asyncio.run(Worker().peer.serve())
