from ouro.sdk.plugins import v3 as sdk
from ouro.sdk.plugins.models import ModelInput, ModelRequest, PluginModels


class ModelReview:
    def __init__(self, models: PluginModels) -> None:
        self.models = models

    async def command(self, call: sdk.CommandCall) -> sdk.CommandOutput:
        model, separator, text = call.argument.strip().partition(" ")
        if not separator or not text.strip():
            raise ValueError("Use model-review <default|provider/endpoint> <text>")
        result = await self.models.generate(
            ModelRequest(
                messages=(ModelInput("user", text),),
                model=model,
                system_prompt=(
                    "Review the supplied text. "
                    "Identify factual gaps and suggest concise improvements."
                ),
            )
        )
        return sdk.CommandOutput(output=result.text)

    async def prepare(self) -> sdk.ContributionSet:
        return sdk.ContributionSet(
            commands=[
                sdk.CommandContribution(
                    "model-review",
                    "Review text using one independent model request.",
                    self.command,
                    usage="<default|provider/endpoint> <text>",
                    timeout_seconds=125,
                )
            ]
        )

    async def start(self) -> None:
        pass

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        pass


def create_plugin(context: sdk.PluginContext) -> ModelReview:
    assert context.models is not None
    return ModelReview(context.models)
