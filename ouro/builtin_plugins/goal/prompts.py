"""Model-facing language for Goal behavior."""

from __future__ import annotations

import json

from .state import GoalRecord

GET_GOAL_DESCRIPTION = (
    "Get the most recent goal for this conversation, including its objective, status, "
    "token budget, token usage, remaining budget, timestamps, and latest report."
)

CREATE_GOAL_DESCRIPTION = (
    "Create a goal only when the user or system/developer instructions explicitly ask for "
    "one. Do not create goals for ordinary tasks, even when they are long or difficult. "
    "Preserve the requested objective without expanding, narrowing, or inventing intent. "
    "Set token_budget only when an explicit budget was provided, and use the exact requested "
    "value. Fails when an unfinished goal already exists. A completed goal does not prevent "
    "creating a new one."
)

UPDATE_GOAL_DESCRIPTION = """Update the current goal only to report that it is complete or
genuinely blocked.

Set status to complete only when the objective has been achieved, the result has been checked,
and no required work remains. The report must summarize the delivered result and the evidence
that it is complete.

Set status to blocked only when the same obstacle has prevented meaningful progress in at least
three consecutive attempts, including the initial attempt and retries, and progress now requires
user input or an external change. The report must summarize completed progress, attempted
approaches, and the exact obstacle.

After the user asks to continue a blocked goal, begin a fresh assessment of repeated obstacles.
Do not use blocked because the work is difficult, slow, uncertain, incomplete, or merely easier
with clarification.

Do not use this tool to pause, resume, replace the objective, change the token budget, or report
that you are stopping. Usage and timestamps are tracked automatically; do not guess them."""

_CREATE = """The user explicitly asked to create a goal.

Call CreateGoal once with objective exactly equal to the next user message. Set token_budget to
{budget} only if that value is a positive integer; when it is null, omit token_budget. Do not
rewrite, summarize, expand, or narrow the objective. Do not start substantive work until the
tool succeeds."""

_CONTINUE = """Continue making concrete, verifiable progress on the current goal.

Goal: {goal}

Do not treat the end of one response as completion. Call UpdateGoal only when its completion or
blocking rules are genuinely satisfied. Current budget: {budget}; tokens used: {used}; tokens
remaining: {remaining}."""

_LIFECYCLE = {
    "paused": (
        "The user paused the current goal. Do not continue it until the user resumes or updates it."
    ),
    "resumed": "The user resumed the current goal. Continue making concrete, verifiable progress.",
    "updated": "The user updated the current goal. Work only toward the updated objective below.",
    "cancelled": "The user cancelled the current goal. Do not continue goal-directed work.",
}


def creation_instruction(token_budget: int | None) -> str:
    return _CREATE.format(budget=_json(token_budget))


def continuation_prompt(goal: GoalRecord, *, tokens_used: int | None) -> str:
    remaining = (
        "unlimited"
        if goal.token_budget is None
        else (max(goal.token_budget - tokens_used, 0) if tokens_used is not None else "unknown")
    )
    return _CONTINUE.format(
        goal=goal.objective,
        budget="unlimited" if goal.token_budget is None else goal.token_budget,
        used=tokens_used if tokens_used is not None else "unknown",
        remaining=remaining,
    )


def lifecycle_prompt(action: str, goal: GoalRecord | None) -> str:
    instruction = _LIFECYCLE[action]
    if goal is None or action == "cancelled":
        return instruction
    budget = "unlimited" if goal.token_budget is None else str(goal.token_budget)
    return f"{instruction}\n\nGoal: {goal.objective}\nTotal token budget: {budget}."


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = [
    "CREATE_GOAL_DESCRIPTION",
    "GET_GOAL_DESCRIPTION",
    "UPDATE_GOAL_DESCRIPTION",
    "continuation_prompt",
    "creation_instruction",
    "lifecycle_prompt",
]
