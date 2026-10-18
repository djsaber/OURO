"""User-owned `/goal` lifecycle command."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from ouro.sdk.plugins.v4 import (
    CommandCall,
    CommandOutput,
    JsonValue,
    PluginSessionControl,
    SessionControlTransition,
    SessionInput,
    SessionInputBatch,
)

from .prompts import creation_instruction, format_goal_status, lifecycle_prompt
from .state import GoalRecord, GoalStateRepository

_BUDGET_PREFIX = re.compile(r"--token-budget(?:\s+|=)(\S+)(?:\s+|$)")
_BUDGET_OPTION = re.compile(r"--(?:no-)?token-budget(?=\s|=|$)")
_LIFECYCLE_COALESCE_KEY = "goal-lifecycle"


@dataclass(frozen=True)
class ParsedGoalCommand:
    action: str
    objective: str | None = None
    token_budget: int | None = None
    clear_budget: bool = False
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
            return CommandOutput(
                output="Paused the current goal.",
                data={"action": "paused"},
                run_action="stop",
                session_control_transitions=(_replace_transition(control, operation="pause"),),
            )
        if parsed.action == "resume":
            if current.status not in {"paused", "blocked", "budget_exhausted"}:
                return _command_error(
                    "goal_not_resumable",
                    f"The current goal is {current.status}; it cannot be resumed.",
                    current,
                    tokens_used=tokens_used,
                )
            token_budget = _effective_budget(parsed, current)
            budget_error = _validate_changed_budget(token_budget, tokens_used)
            if budget_error is not None:
                return _command_error(
                    "invalid_token_budget", budget_error, current, tokens_used=tokens_used
                )
            return CommandOutput(
                output="Resumed the current goal.",
                data={"action": "resumed"},
                run_action="continue",
                session_control_transitions=(
                    _replace_transition(
                        control,
                        operation="resume",
                        token_budget=token_budget,
                    ),
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
            token_budget = _effective_budget(parsed, current)
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
        output="Creating the requested goal.",
        data={"objective": objective, "token_budget": token_budget},
        session_input_batches=(
            SessionInputBatch(
                inputs=(
                    SessionInput(
                        prompt=creation_instruction(objective, token_budget),
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
                coalesce_key=_LIFECYCLE_COALESCE_KEY,
            ),
        ),
        session_control_transitions=(
            SessionControlTransition(
                action="acquire",
                controller_id="mode",
                payload={
                    "operation": "create",
                    "objective": objective,
                    "token_budget": token_budget,
                },
                reason="user_created_goal",
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
    parts = text.split(maxsplit=1)
    head, tail = parts[0], parts[1] if len(parts) == 2 else ""
    command = head.casefold()
    if command in {"status", "pause", "cancel"}:
        if tail.strip():
            return ParsedGoalCommand(
                action=command,
                error=f"/goal {command} does not accept additional arguments.",
            )
        return ParsedGoalCommand(action=command)
    if command == "resume":
        budget, clear_budget, remainder, error = _parse_budget_prefix(tail.strip())
        if error is not None:
            return ParsedGoalCommand(action="resume", error=error)
        if remainder:
            return ParsedGoalCommand(
                action="resume",
                error="Usage: /goal resume [--token-budget N | --no-token-budget]",
            )
        return ParsedGoalCommand(action="resume", token_budget=budget, clear_budget=clear_budget)
    if command == "replace":
        budget, clear_budget, objective, error = _parse_budget_prefix(tail.strip())
        if error is not None:
            return ParsedGoalCommand(action="replace", error=error)
        if not objective:
            return ParsedGoalCommand(
                action="replace",
                error="Usage: /goal replace [--token-budget N | --no-token-budget] <objective>",
            )
        return ParsedGoalCommand(
            action="replace", objective=objective, token_budget=budget, clear_budget=clear_budget
        )
    budget, clear_budget, objective, error = _parse_budget_prefix(text)
    if error is not None:
        return ParsedGoalCommand(action="create", error=error)
    if not objective:
        return ParsedGoalCommand(action="create", error="Goal objective cannot be empty.")
    return ParsedGoalCommand(
        action="create", objective=objective, token_budget=budget, clear_budget=clear_budget
    )


def _parse_budget_prefix(text: str) -> tuple[int | None, bool, str, str | None]:
    budget = None
    clear_budget = False
    option = _BUDGET_OPTION.match(text)
    if option is None:
        return None, False, text, None
    if option.group() == "--no-token-budget":
        if text[option.end() :].startswith("="):
            return None, False, "", "The --no-token-budget option does not accept a value."
        clear_budget = True
        remainder = text[option.end() :].strip()
    else:
        match = _BUDGET_PREFIX.match(text)
        if match is None:
            return None, False, "", "Token budget syntax is --token-budget N."
        raw_budget = match.group(1)
        if not raw_budget.isdecimal() or int(raw_budget) <= 0:
            return None, False, "", "Token budget must be a positive integer."
        budget = int(raw_budget)
        remainder = text[match.end() :].strip()
    if _BUDGET_OPTION.match(remainder):
        return None, False, "", "Specify only one token budget option."
    return budget, clear_budget, remainder, None


def _effective_budget(parsed: ParsedGoalCommand, current: GoalRecord) -> int | None:
    if parsed.clear_budget:
        return None
    return parsed.token_budget if parsed.token_budget is not None else current.token_budget


def _validate_changed_budget(token_budget: int | None, tokens_used: int | None) -> str | None:
    if token_budget is None:
        return None
    if tokens_used is None:
        return "Token usage is unavailable; a total budget cannot be validated."
    if token_budget <= tokens_used:
        return f"The total token budget must be greater than tokens used ({tokens_used})."
    return None


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
