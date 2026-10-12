"""A deterministic model backend using only the public protocol contribution API."""

from ouro.sdk.plugins.models import ModelUsage
from ouro.sdk.plugins.providers import (
    ProviderCheck,
    ProviderCompletion,
    ProviderContribution,
    ProviderEvent,
    ProviderOutput,
    ProviderRequest,
)
from ouro.sdk.plugins.v3 import ContributionSet, PluginContext, StopReason


def validate(request: ProviderRequest) -> None:
    if request.connection.request_options or request.connection.reasoning_profile:
        raise ValueError("Faux does not accept protocol options")
    if request.model.input_modalities:
        raise ValueError("Faux accepts text input only")
    if request.model.reasoning.mode != "default" or request.model.reasoning.effort is not None:
        raise ValueError("Faux does not support reasoning controls")


async def stream(request: ProviderRequest, output: ProviderOutput) -> None:
    validate(request)
    await output(ProviderEvent("text_delta", text="Hello from OURO."))
    await output(
        ProviderEvent("done", usage=ModelUsage(1, 4), completion=ProviderCompletion("stop"))
    )


async def check(request: ProviderRequest) -> ProviderCheck:
    validate(request)
    return ProviderCheck(True, "Faux protocol is ready")


class FauxPlugin:
    async def prepare(self) -> ContributionSet:
        return ContributionSet(providers=[ProviderContribution("faux", stream, check)])

    async def start(self) -> None:
        pass

    async def stop(self, reason: StopReason, deadline: float) -> None:
        pass


def create_plugin(context: PluginContext) -> FauxPlugin:
    return FauxPlugin()
