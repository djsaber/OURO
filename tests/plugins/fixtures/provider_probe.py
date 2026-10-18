import asyncio
import json
from dataclasses import asdict

from ouro.sdk.plugins import v4 as sdk
from ouro.sdk.plugins.models import ModelInput, ModelRequest, ModelUsage
from ouro.sdk.plugins.providers import (
    ProviderCheck,
    ProviderCompletion,
    ProviderContribution,
    ProviderEvent,
    ProviderRetryNotice,
    ProviderToolDelta,
)
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_drivers import WorkerRunDrivers
from ouro.sdk.plugins.worker_models import WorkerModels
from ouro.sdk.plugins.worker_providers import WorkerProviders


async def stream(request, output):
    if request.model.model_id == "diagnostics":
        await output(
            ProviderEvent(
                "retry_scheduled",
                retry=ProviderRetryNotice(2, 2, 0, f"Retry {request.connection.api_key}"),
            )
        )
        await output(
            ProviderEvent(
                "error",
                error=f"Failure {request.connection.api_key}",
                error_code=request.connection.api_key,
            )
        )
        return
    if request.model.model_id == "mutate":
        value = request.connection.request_options["nested"]["value"]
        request.connection.request_options["nested"]["value"] = "changed"
        await output(ProviderEvent("text_delta", text=value))
        await output(ProviderEvent("done", completion=ProviderCompletion("stop")))
        return
    if request.model.model_id == "tools":
        results = [item for item in request.messages if item.tool_result is not None]
        if not results:
            await output(
                ProviderEvent(
                    "tool_call_delta",
                    tool_call_delta=ProviderToolDelta(
                        0,
                        "read-1",
                        "Read",
                        json.dumps({"path": request.connection.request_options["path"]}),
                    ),
                )
            )
            await output(
                ProviderEvent(
                    "done", usage=ModelUsage(7, 3), completion=ProviderCompletion("tool_call")
                )
            )
            return
        await output(ProviderEvent("text_delta", text=results[-1].content[0].text))
        await output(
            ProviderEvent("done", usage=ModelUsage(7, 3), completion=ProviderCompletion("stop"))
        )
        return
    if request.model.model_id == "error":
        raise ValueError(f"Failure {request.connection.api_key}")
    if request.model.model_id == "inspect":
        text = json.dumps(asdict(request))
    else:
        text = "provider answer"
    await output(ProviderEvent("text_delta", text=text))
    if request.model.model_id == "wait":
        await asyncio.Event().wait()
    await output(
        ProviderEvent("done", usage=ModelUsage(7, 3), completion=ProviderCompletion("stop"))
    )


async def check(request):
    if request.model.model_id == "diagnostics":
        return ProviderCheck(
            False, f"Check {request.connection.api_key}", request.connection.api_key
        )
    return ProviderCheck(True, "probe ready")


async def drive(context, resume):
    response = await context.models.request(prompt="review", tools=())
    return sdk.DriverFinished(response.text, response_id=response.response_id)


class Probe:
    def __init__(self, models):
        self.models = models

    async def command(self, call):
        result = await self.models.generate(ModelRequest((ModelInput("user", "review"),)))
        return sdk.CommandOutput(result.text)

    async def stream(self, request, output):
        if request.model.model_id != "scope":
            return await stream(request, output)
        failures = []
        for operation in (
            self.models.request(prompt="nested"),
            self.models.generate(ModelRequest((ModelInput("user", "nested"),))),
        ):
            try:
                await asyncio.wait_for(operation, 2)
            except Exception as error:
                failures.append(str(error))
        await output(ProviderEvent("text_delta", text=json.dumps(failures)))
        await output(ProviderEvent("done", completion=ProviderCompletion("stop")))

    async def prepare(self):
        return sdk.ContributionSet(
            providers=[ProviderContribution("probe", self.stream, check)],
            run_drivers=[sdk.RunDriverContribution("review", drive)],
            commands=[sdk.CommandContribution("generate", "generate", self.command)],
        )

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def create_plugin(context):
    return Probe(context.models)


class Worker:
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
        self.providers = WorkerProviders(self.peer)
        self.drivers = WorkerRunDrivers(self.peer, [sdk.RunDriverContribution("review", drive)])
        self.peer.handlers["drivers.call"] = lambda params: self.drivers.dispatch(
            "drivers.call", params
        )
        self.models = WorkerModels(self.peer)
        self.peer.handlers["models.output"] = self.models.output
        self.probe = Probe(self.models)

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        contributions = await self.probe.prepare()
        return {
            "providers": self.providers.register(contributions.providers),
            "run_drivers": ["review"],
            "commands": ["generate"],
        }

    async def lifecycle(self, params):
        return {}

    async def command(self, params):
        return asdict(await self.probe.command(None))


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
