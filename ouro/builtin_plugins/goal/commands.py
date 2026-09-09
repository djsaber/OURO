"""User-owned `/goal` lifecycle command."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from ouro.sdk.plugins.v2 import (
    CommandCall,
    CommandOutput,
    JsonValue,
    PluginSessionControl,
    SessionControlTransition,
    SessionInput,
    SessionInputBatch,
)

from .prompts import creation_instruction, lifecycle_prompt
from .state import GoalRecord, GoalStateRepository

_BUDGET_PREFIX = re.compile(r"--token-budget(?:\s+|=)(\S+)(?:\s+|$)")
_LIFECYCLE_COALESCE_KEY = "goal-lifecycle"


@dataclass(frozen=True)
class ParsedGoalCommand:
    action: str
    objective: str | None = None
    token_budget: int | None = None
    error: str | None = None


class GoalCommandService:
    def __init__(
        self,
        state: GoalStateRepository,
    ) -> None:
        self._state = state

    def handle(self, call: CommandCall) -> CommandOutput:
        if call.session_id is None:
            return CommandOutput(output="The /goal command requires a target conversation.")
        parsed = parse_goal_command(call.argument)
        if parsed.error is not None:
            return CommandOutput(output=parsed.error, data={"error": "invalid_command"})
        current = self._state.load().goal
        control = self._state.control()
        tokens_used = self._state.tokens_used(current) if current is not None else None

        if parsed.action == "status":
            if current is None:
                return CommandOutput(output="No goal exists.", data={"goal": None})
            return CommandOutput(
                output=format_goal_status(current, tokens_used=tokens_used),
                data={"goal": current.to_public(tokens_used=tokens_used)},
            )
        if parsed.action == "create":
            assert parsed.objective is not None
            if current is not None and current.status != "complete":
                return _command_error(
                    "unfinished_goal_exists",
                    f"The current goal is {current.status}; use /goal replace or /goal cancel.",
                    current,
                    tokens_used=tokens_used,
                )
            return _creation_output(parsed.objective, parsed.token_budget)
        if current is None:
            return _command_error("goal_not_found", "No goal exists.")
        if parsed.action == "pause":
            if current.status != "active":
                return _command_error(
                    "goal_not_active",
                    f"The current goal is {current.status}; it cannot be paused.",
                    current,
                    tokens_used=tokens_used,
                )
            return _lifecycle_output(
                current,
                action="paused",
                output="Pausing the current goal.",
                transition=_replace_transition(control, operation="pause"),
            )
        if parsed.action == "resume":
            if current.status not in {"paused", "blocked", "budget_exhausted"}:
                return _command_error(
                    "goal_not_resumable",
                    f"The current goal is {current.status}; it cannot be resumed.",
                    current,
                    tokens_used=tokens_used,
                )
            token_budget = parsed.token_budget
            budget_error = _validate_changed_budget(token_budget, tokens_used)
            if budget_error is not None:
                return _command_error(
                    "invalid_token_budget", budget_error, current, tokens_used=tokens_used
                )
            resumed = replace(current, status="active", token_budget=token_budget, report=None)
            return _lifecycle_output(
                resumed,
                action="resumed",
                output="Resuming the current goal.",
                transition=_replace_transition(
                    control,
                    operation="resume",
                    token_budget=token_budget,
                ),
            )
        if parsed.action == "replace":
            if current.status == "complete":
                return _command_error(
                    "goal_complete",
                    "The latest goal is complete; use /goal <objective> to create a new one.",
                    current,
                    tokens_used=tokens_used,
                )
            assert parsed.objective is not None
            token_budget = parsed.token_budget
            budget_error = _validate_changed_budget(token_budget, tokens_used)
            if budget_error is not None:
                return _command_error(
                    "invalid_token_budget", budget_error, current, tokens_used=tokens_used
                )
            updated = replace(
                current,
                objective=parsed.objective,
                status="active",
                token_budget=token_budget,
                report=None,
            )
            return _lifecycle_output(
                updated,
                action="updated",
                output="Updating the current goal.",
                transition=_replace_transition(
                    control,
                    operation="replace",
                    objective=parsed.objective,
                    token_budget=token_budget,
                ),
            )
        if parsed.action == "cancel":
            if control is None:
                self._state.delete()
                return _lifecycle_output(
                    None,
                    action="cancelled",
                    output="Cancelled the latest goal.",
                )
            return _lifecycle_output(
                None,
                action="cancelled",
                output="Cancelling the current goal.",
                transition=SessionControlTransition(
                    action="release",
                    controller_id="mode",
                    activation_id=control.activation_id,
                    payload={"operation": "cancel"},
                    reason="user_cancelled_goal",
                ),
            )
        raise AssertionError(f"unsupported parsed Goal command: {parsed.action}")


def _creation_output(objective: str, token_budget: int | None) -> CommandOutput:
    return CommandOutput(
        output="Requesting goal creation.",
        data={"objective": objective, "token_budget": token_budget},
        session_input_batches=(
            SessionInputBatch(
                inputs=(
                    SessionInput(
                        prompt=creation_instruction(token_budget),
                        mode="follow_up",
                        title=f"Create Goal: {objective[:48]}",
                        role="user",
                        scaffold=True,
                        semantic_tag="goal-create",
                    ),
                    SessionInput(
                        prompt=objective,
                        mode="follow_up",
                        role="user",
                        scaffold=False,
                    ),
                ),
            ),
        ),
    )


def _lifecycle_output(
    goal: GoalRecord | None,
    *,
    action: str,
    output: str,
    transition: SessionControlTransition | None = None,
) -> CommandOutput:
    return CommandOutput(
        output=output,
        data={"action": action},
        session_input_batches=(
            SessionInputBatch(
                inputs=(
                    SessionInput(
                        prompt=lifecycle_prompt(action, goal),
                        mode="follow_up",
                        role="user",
                        scaffold=True,
                        semantic_tag=f"goal-{action}",
                    ),
                ),
                coalesce_key=_LIFECYCLE_COALESCE_KEY,
            ),
        ),
        session_control_transitions=((transition,) if transition is not None else ()),
    )


def _replace_transition(
    control: PluginSessionControl | None,
    *,
    operation: str,
    objective: str | None = None,
    token_budget: int | None = None,
) -> SessionControlTransition:
    payload: dict[str, JsonValue] = {"operation": operation}
    if objective is not None:
        payload["objective"] = objective
    if operation in {"resume", "replace"}:
        payload["token_budget"] = token_budget
    return SessionControlTransition(
        action="replace" if control is not None else "acquire",
        controller_id="mode",
        replace_activation_ids=(control.activation_id,) if control is not None else (),
        payload=payload,
        reason=f"user_{operation}d_goal",
    )


def parse_goal_command(argument: str) -> ParsedGoalCommand:
    text = argument.strip()
    if not text or text.casefold() == "status":
        return ParsedGoalCommand(action="status")
    head, _, tail = text.partition(" ")
    command = head.casefold()
    if command in {"pause", "cancel"}:
        if tail.strip():
            return ParsedGoalCommand(
                action=command,
                error=f"/goal {command} does not accept additional arguments.",
            )
        return ParsedGoalCommand(action=command)
    if command == "resume":
        budget, remainder, error = _parse_budget_prefix(tail.strip())
        if error is not None:
            return ParsedGoalCommand(action="resume", error=error)
        if remainder:
            return ParsedGoalCommand(
                action="resume",
                error="Usage: /goal resume [--token-budget N]",
            )
        return ParsedGoalCommand(action="resume", token_budget=budget)
    if command == "replace":
        budget, objective, error = _parse_budget_prefix(tail.strip())
        if error is not None:
            return ParsedGoalCommand(action="replace", error=error)
        if not objective:
            return ParsedGoalCommand(
                action="replace",
                error="Usage: /goal replace [--token-budget N] <objective>",
            )
        return ParsedGoalCommand(action="replace", objective=objective, token_budget=budget)
    budget, objective, error = _parse_budget_prefix(text)
    if error is not None:
        return ParsedGoalCommand(action="create", error=error)
    if not objective:
        return ParsedGoalCommand(action="create", error="Goal objective cannot be empty.")
    return ParsedGoalCommand(action="create", objective=objective, token_budget=budget)


def _parse_budget_prefix(text: str) -> tuple[int | None, str, str | None]:
    if not text.startswith("--token-budget"):
        return None, text, None
    match = _BUDGET_PREFIX.match(text)
    if match is None:
        return None, "", "Token budget syntax is --token-budget N."
    raw_budget = match.group(1)
    if not raw_budget.isdecimal() or int(raw_budget) <= 0:
        return None, "", "Token budget must be a positive integer."
    return int(raw_budget), text[match.end() :].strip(), None


def _validate_changed_budget(token_budget: int | None, tokens_used: int | None) -> str | None:
    if token_budget is None:
        return None
    if tokens_used is None:
        return "Token usage is unavailable; a total budget cannot be validated."
    if token_budget <= tokens_used:
        return f"The total token budget must be greater than tokens used ({tokens_used})."
    return None


def format_goal_status(goal: GoalRecord, *, tokens_used: int | None) -> str:
    budget = "unlimited" if goal.token_budget is None else str(goal.token_budget)
    remaining = (
        "unlimited"
        if goal.token_budget is None
        else (
            str(max(goal.token_budget - tokens_used, 0)) if tokens_used is not None else "unknown"
        )
    )
    used = str(tokens_used) if tokens_used is not None else "unknown"
    lines = [
        f"Goal: {goal.status}",
        f"Objective: {goal.objective}",
        f"Usage: {used} tokens; budget: {budget}; remaining: {remaining}.",
        f"Created: {goal.created_at}",
        f"Updated: {goal.updated_at}",
    ]
    if goal.report is not None:
        lines.append(f"Report: {goal.report}")
    return "\n".join(lines)


def _command_error(
    error: str,
    message: str,
    goal: GoalRecord | None = None,
    *,
    tokens_used: int | None = None,
) -> CommandOutput:
    return CommandOutput(
        output=message,
        data={
            "error": error,
            "goal": goal.to_public(tokens_used=tokens_used) if goal is not None else None,
        },
    )


__all__ = [
    "GoalCommandService",
    "ParsedGoalCommand",
    "format_goal_status",
    "parse_goal_command",
]
