"""Model-facing Goal instructions, status projections, and stop reports."""

from __future__ import annotations

from .state import GoalRecord

GET_GOAL_DESCRIPTION = (
    "Get this conversation's current goal, status, report, timestamps, and token usage. "
    "The budget covers total input and output tokens. Unavailable usage is not zero."
)

CREATE_GOAL_DESCRIPTION = (
    "Create a goal for ongoing work only when the user or system/developer instructions "
    "explicitly request one. Ordinary tasks or requests to draft a goal do not authorize "
    "creation. Preserve the requested scope; set token_budget only if explicitly requested. "
    "Fails if an unfinished goal exists. If the requested goal is already active, continue "
    "working without recreating it."
)

UPDATE_GOAL_DESCRIPTION = """Resume the current goal or mark it complete or blocked.
Always include report.

- active: Resume a paused or blocked goal when the current request and evidence justify it.
  Explain why, wait for success, then continue from existing progress. A new message alone does
  not resume the goal. An exhausted budget or unavailable budget usage requires user action.
- complete: The objective is achieved, verified, and no required work remains. Report the
  delivered result and verification evidence.
- blocked: The same obstacle has prevented meaningful progress in at least three consecutive
  goal turns, and progress requires user input or an external change. Report progress, attempts,
  the obstacle, and what is needed to continue.

A goal turn is one work cycle started by a user request to start or resume the goal or an
automatic request to continue. Tool retries within it do not count as separate turns. After
resumption, count from zero. Do not repeat ineffective actions just to reach three turns.
Difficulty, uncertainty, an intermediate answer, or a low budget alone does not justify stopping.
When complete or blocked applies, call this tool. After a successful update, report the returned
status and stop goal work.
This tool cannot pause, change the objective or budget, or declare a budget limit. Usage and
timestamps are recorded automatically."""

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
    """Describe Goal state at this conversation boundary."""
    return "Goal state after the latest operation.\n\n" + _goal_snapshot(goal)


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
