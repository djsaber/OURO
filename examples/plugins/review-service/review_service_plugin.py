from ouro.sdk.plugins import v4 as sdk
from ouro.sdk.plugins.services import ServiceCall


class ReviewService:
    def __init__(self, models: sdk.RunModels, tools: sdk.RunTools) -> None:
        self.models, self.tools = models, tools

    async def inspect(self, call: ServiceCall) -> dict:
        response = await self.models.request(
            prompt="Review the user's request before acting.", tools=()
        )
        return {"text": response.text, "response_id": response.response_id}

    async def act(self, call: ServiceCall) -> dict:
        response = await self.models.request(prompt="Act on the review and the user's request.")
        if not response.tool_calls:
            return {"suspended": False, "text": response.text, "response_id": response.response_id}
        result = await self.tools.execute_model_response(
            response.response_id, checkpoint={"step": "summarize", "review": call.arguments}
        )
        return {
            "suspended": result.suspended,
            "text": "\n".join(
                item.content for item in result.results if isinstance(item.content, str)
            ),
        }

    async def prepare(self) -> sdk.ContributionSet:
        return sdk.ContributionSet(
            services=[sdk.ServiceContribution("review", {"inspect": self.inspect, "act": self.act})]
        )

    async def start(self) -> None:
        pass

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        pass


def create_plugin(context: sdk.PluginContext) -> ReviewService:
    assert context.models is not None and context.tools is not None
    return ReviewService(context.models, context.tools)
