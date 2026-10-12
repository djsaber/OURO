import asyncio

from faux_provider_plugin import FauxPlugin

from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_providers import WorkerProviders


class Worker:
    def __init__(self) -> None:
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
            },
            max_requests=1,
        )
        self.providers = WorkerProviders(self.peer)
        self.plugin = FauxPlugin()

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        contributions = await self.plugin.prepare()
        return {"providers": self.providers.register(contributions.providers)}

    async def lifecycle(self, params):
        return {}


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
