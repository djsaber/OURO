import asyncio

from model_review_plugin import ModelReview

from ouro.sdk.plugins import v5 as sdk
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_models import WorkerModels


class Worker:
    def __init__(self) -> None:
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
        self.models = WorkerModels(self.peer)
        self.peer.handlers["models.output"] = self.models.output
        self.plugin = ModelReview(self.models)

    async def initialize(self, params):
        if "models" not in params["features"]:
            raise RuntimeError("This Host does not provide model requests")
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"commands": ["model-review"]}

    async def lifecycle(self, params):
        return {}

    async def command(self, params):
        call = params["call"]
        result = await self.plugin.command(sdk.CommandCall(call["command_id"], call["argument"]))
        return {"output": result.output}


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
