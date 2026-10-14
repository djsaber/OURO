"""Model-facing Goal instructions, status projections, and stop reports."""

from __future__ import annotations

from .state import GoalRecord

GET_GOAL_DESCRIPTION = (
    "Get the latest goal for this conversation: objective, status, total token budget, "
    "actual usage, remaining budget, timestamps, and report. Unavailable usage is not zero."
)

CREATE_GOAL_DESCRIPTION = (
    "Create a goal only when the user or system/developer instructions explicitly request one, "
    "including a natural-language request. Ordinary tasks and requests to draft a goal do not "
    "authorize creation. Preserve the requested objective without expanding or narrowing it. "
    "Set token_budget only to an explicitly requested positive total. An unfinished goal "
    "prevents creation. If the user has already created the goal, begin work without calling "
    "this tool again. After success, follow the current goal instructions."
)

UPDATE_GOAL_DESCRIPTION = """Resume the current goal or report that it is complete or blocked.

Call with status="active" to resume a paused or blocked goal when the current request and
available evidence justify continuing. Explain why in report, then continue from existing progress.
Receiving a message or continuing the conversation does not itself resume the goal.
Preserve the objective and budget. An exhausted or unverifiable budget requires the user's action.

Call with status="complete" when the objective has been achieved, verified against concrete
evidence, and no required work remains. Include the delivered result and verification in report.

Call with status="blocked" only after the same obstacle has prevented meaningful progress in
at least three consecutive goal turns and further progress requires user input or an external
change. A goal turn is work and its result check initiated by a user request to start or resume
the goal, or by an automatic request to continue. Tool retries within one turn are not
separate turns.
Report progress, attempted approaches, the obstacle, and what would allow work to continue.

After a blocked goal is resumed, start a fresh assessment; earlier turns do not count.
Do not repeat ineffective actions merely to reach three turns. Difficulty, uncertainty, an
intermediate response, or a nearly exhausted budget does not establish completion or blocking.
When a stopping condition is satisfied, call this tool rather than only describing that state.
After successfully setting complete or blocked, report the outcome and stop goal work.

This tool cannot pause, change the objective or budget, or declare a budget limit.
Usage and timestamps are recorded automatically; do not guess them."""

_ACTIVE_INSTRUCTIONS = """Work toward this objective using the conversation and available evidence.
Preserve the user's scope and constraints. Continue from existing progress.

Check whether the requested outcome is already supported by concrete evidence. If it is achieved
and no required work remains, call UpdateGoal with status="complete" and the result and evidence.
Otherwise, take the next useful action. An intermediate answer is not completion.

If the same obstacle prevents meaningful progress for three consecutive goal turns and progress
requires user input or an external change, call UpdateGoal with status="blocked", the progress,
attempts, obstacle, and what is needed to continue. A turn is one work cycle initiated by a user
request to start or resume this goal or an automatic request to continue; tool retries are
not new turns.
Do not repeat ineffective actions just to count turns. After either successful update, report
the outcome and stop goal work. Budget exhaustion is not completion."""

_STOPPED_INSTRUCTIONS = """The goal is stopped. Handle the user's current request normally.
Use tools as needed. Continuing the conversation does not reactivate the goal or remove its budget.
Before continuing goal work, decide whether the current request and evidence justify resumption.
If resumption is justified and the goal is paused or blocked, call UpdateGoal with status="active"
and explain why.
Proceed with goal work only after it succeeds. An exhausted budget requires the user's action.
The user can also use /goal resume. Budget changes require the user.
The user can remove the goal with /goal cancel."""

_EVENTS = {
    "updated": (
        "The user has changed this goal. Use the updated objective below, keeping relevant "
        "progress and constraints. Start a fresh assessment of obstacles for this objective."
    ),
    "cancelled": (
        "The user has cancelled this goal. There is no current goal. "
        "Handle subsequent user requests normally."
    ),
}


def budget_description(token_budget: int | None) -> str:
    return "No token budget is set." if token_budget is None else f"{token_budget} tokens in total."


def creation_instruction(objective: str, token_budget: int | None) -> str:
    return (
        "The user has created this goal. It is already active; do not call CreateGoal again.\n\n"
        f"Objective:\n{objective}\n\nToken budget: {budget_description(token_budget)}\n\n"
        + _ACTIVE_INSTRUCTIONS
    )


def goal_context(goal: GoalRecord) -> str:
    """Project stable business facts without changing counters in the request prefix."""
    return (
        "Current goal state for this request. Stored goal notifications describe past events.\n\n"
        + _goal_snapshot(goal)
    )


def _goal_snapshot(goal: GoalRecord) -> str:
    lines = [
        f"Status: {goal.status}",
        f"Objective:\n{goal.objective}",
        f"Token budget: {budget_description(goal.token_budget)}",
    ]
    if goal.report:
        lines.append(f"Latest report:\n{goal.report}")
    if goal.status == "active":
        lines.append(_ACTIVE_INSTRUCTIONS)
    elif goal.status == "complete":
        lines.append("This goal is complete. Handle the user's current request normally.")
    else:
        lines.append(_STOPPED_INSTRUCTIONS)
    return "\n\n".join(lines)


def continuation_prompt(goal: GoalRecord, *, tokens_used: int | None) -> str:
    return (
        "Automatically continuing the existing goal under the user's earlier request. "
        "Review the latest results and continue from the current progress.\n\n"
        f"Objective:\n{goal.objective}\n\n"
        f"{usage_description(goal.token_budget, tokens_used)}\n\n" + _ACTIVE_INSTRUCTIONS
    )


def lifecycle_prompt(action: str, goal: GoalRecord | None) -> str:
    instruction = _EVENTS[action]
    if goal is None or action == "cancelled":
        return instruction
    return f"{instruction}\n\nState after this action:\n\n{_goal_snapshot(goal)}"


def usage_description(token_budget: int | None, tokens_used: int | None) -> str:
    used = str(tokens_used) if tokens_used is not None else "unavailable"
    remaining = (
        "not limited by a token budget"
        if token_budget is None
        else str(max(token_budget - tokens_used, 0))
        if tokens_used is not None
        else "unavailable"
    )
    return (
        f"Token budget: {budget_description(token_budget)} "
        f"Tokens used: {used}; tokens remaining: {remaining}."
    )


def format_goal_status(goal: GoalRecord, *, tokens_used: int | None) -> str:
    lines = [
        f"Goal: {goal.status}",
        f"Objective: {goal.objective}",
        usage_description(goal.token_budget, tokens_used),
        f"Created: {goal.created_at}",
        f"Updated: {goal.updated_at}",
    ]
    if goal.report:
        lines.append(f"Report: {goal.report}")
    return "\n".join(lines)


def runtime_stop_report(status: str) -> str:
    cause = {
        "aborted": "Work was interrupted.",
        "failed": "Execution failed before the work could finish.",
        "paused": "Execution stopped before the work could finish.",
        "recoverable": "Execution was interrupted and requires recovery.",
    }[status]
    return f"{cause} The goal is paused. Review the issue before using /goal resume."


def budget_stop_report(tokens_used: int, token_budget: int) -> str:
    return (
        f"The goal's token budget was exhausted: {tokens_used} used of {token_budget}. "
        "This does not mean the objective is complete. Use /goal resume --token-budget N "
        "with a total greater than usage, or /goal resume --no-token-budget."
    )


USAGE_UNAVAILABLE_REPORT = (
    "Token usage is unavailable, so budgeted goal work has been paused. "
    "Restore usage reporting before resuming with a budget, or explicitly remove the limit "
    "with /goal resume --no-token-budget."
)
NO_TOOL_CALLS_REPORT = (
    "Automatic work ended without a tool call and the goal is still unfinished. "
    "Automatic continuation has been paused to avoid repeating responses. "
    "Review the latest response and use /goal resume when ready to continue."
)
