from __future__ import annotations

from ouro.sdk.plugins import v5 as sdk


def request_review(call: sdk.CommandCall) -> sdk.CommandOutput:
    return sdk.CommandOutput(
        session_inputs=(
            sdk.SessionInput(
                prompt=call.argument,
                driver_id="dev.ouro.examples.review_driver/run_drivers/review",
            ),
        )
    )


async def review(
    context: sdk.RunDriverContext, resume: sdk.DriverResume | None
) -> sdk.DriverFinished | sdk.DriverSuspended:
    if resume is not None:
        return sdk.DriverFinished(
            "\n".join(item.content for item in resume.results if isinstance(item.content, str))
        )
    review = await context.models.request(
        prompt="Review the user's request before acting.", tools=()
    )
    action = await context.models.request(prompt="Act on the review and the user's request.")
    if not action.tool_calls:
        return sdk.DriverFinished(action.text, response_id=action.response_id)
    result = await context.tools.execute_model_response(
        action.response_id, checkpoint={"step": "summarize", "review": review.text}
    )
    if result.suspended:
        return sdk.DriverSuspended()
    return sdk.DriverFinished(
        "\n".join(item.content for item in result.results if isinstance(item.content, str))
    )


class ReviewPlugin:
    async def prepare(self) -> sdk.ContributionSet:
        return sdk.ContributionSet(
            run_drivers=[sdk.RunDriverContribution("review", review)],
            commands=[
                sdk.CommandContribution("review", "Review and execute a request", request_review)
            ],
        )

    async def start(self) -> None:
        pass

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        pass


def create_plugin(context: sdk.PluginContext) -> ReviewPlugin:
    return ReviewPlugin()
