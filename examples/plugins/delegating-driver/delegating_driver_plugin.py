from __future__ import annotations

from ouro.sdk.plugins import v5 as sdk


def summarize(results: tuple[sdk.NativeToolResult, ...]) -> str:
    return "\n".join(
        result.content
        if isinstance(result.content, str)
        else "\n".join(
            part.text for part in result.content if isinstance(part, sdk.ToolTextContent)
        )
        for result in results
    )


async def delegate(
    context: sdk.RunDriverContext, resume: sdk.DriverResume | None
) -> sdk.DriverFinished | sdk.DriverSuspended:
    if resume is not None:
        if resume.checkpoint == "finish":
            return sdk.DriverFinished(summarize(resume.results))
        if resume.checkpoint != "review":
            raise ValueError("unsupported delegation checkpoint")
    else:
        brief = await context.models.request(
            prompt="Write a self-contained task brief for independent readers of this request.",
            tools=(),
        )
        result = await context.agents.spawn(
            (
                sdk.AgentTask("reader", f"Find supporting evidence.\n{brief.text}", "Evidence"),
                sdk.AgentTask("reader", f"Find risks and counterexamples.\n{brief.text}", "Review"),
            ),
            checkpoint="review",
        )
        if result.suspended:
            return sdk.DriverSuspended()

    review = await context.models.request(
        prompt="Review both native Agent results. Act if needed, otherwise give the final answer."
    )
    if not review.tool_calls:
        return sdk.DriverFinished(review.text, response_id=review.response_id)
    result = await context.tools.execute_model_response(review.response_id, checkpoint="finish")
    if result.suspended:
        return sdk.DriverSuspended()
    return sdk.DriverFinished(summarize(result.results))


class DelegatingPlugin:
    async def prepare(self) -> sdk.ContributionSet:
        return sdk.ContributionSet(run_drivers=[sdk.RunDriverContribution("delegate", delegate)])

    async def start(self) -> None:
        pass

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        pass


def create_plugin(context: sdk.PluginContext) -> DelegatingPlugin:
    return DelegatingPlugin()
