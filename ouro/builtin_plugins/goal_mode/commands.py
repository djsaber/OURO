"""User-owned `/goal` lifecycle command."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass

from ouro.sdk.plugins.v1 import (
    CommandCall,
    CommandOutput,
    SessionControlTransition,
    SessionInput,
)

from .prompts import continuation_prompt, goal_creation_request_prompt
from .state import (
    UNFINISHED_GOAL_STATUSES,
    GoalActor,
    GoalRecord,
    GoalStateRepository,
    GoalStatus,
)

_BUDGET_PREFIX = re.compile(r"--token-budget(?:\s+|=)(\S+)(?:\s+|$)")


@dataclass(frozen=True)
class ParsedGoalCommand:
    action: str
    source_request: str | None = None
    token_budget: int | None = None
    error: str | None = None


class GoalCommandService:
    def __init__(
        self,
        state: GoalStateRepository,
        lock: AbstractContextManager[object],
    ) -> None:
        self._state = state
        self._lock = lock

    def handle(self, call: CommandCall) -> CommandOutput:
        if call.session_id is None:
            return CommandOutput(output="The /goal command requires a target Session.")
        parsed = parse_goal_command(call.argument)
        if parsed.error is not None:
            return CommandOutput(output=parsed.error, data={"error": "invalid_command"})
        with self._lock:
            current = self._state.load(call.session_id).goal

        if parsed.action == "status":
            if current is None:
                return CommandOutput(output="No Goal exists for this Session.", data={"goal": None})
            return CommandOutput(
                output=format_goal_status(current),
                data={"goal": current.to_public()},
            )
        if parsed.action == "create":
            assert parsed.source_request is not None
            if current is not None and current.status in UNFINISHED_GOAL_STATUSES:
                return CommandOutput(
                    output=(
                        f"Goal {current.goal_id} is {current.status}. Use /goal replace to "
                        "replace it explicitly, or pause/cancel it first."
                    ),
                    data={"error": "unfinished_goal_exists", "goal": current.to_public()},
                )
            return self._creation_request_output(
                source_request=parsed.source_request,
                token_budget=parsed.token_budget,
            )
        if current is None:
            return CommandOutput(
                output="No Goal exists for this Session.", data={"error": "goal_not_found"}
            )
        if parsed.action == "pause":
            if current.status != "active" or current.activation_id is None:
                return CommandOutput(
                    output=f"Goal {current.goal_id} is {current.status}; it cannot be paused.",
                    data={"error": "goal_not_active", "goal": current.to_public()},
                )
            return CommandOutput(
                output=f"Pausing Goal {current.goal_id}.",
                data={"goal_id": current.goal_id, "status": "paused"},
                session_control_transitions=(
                    self._release_transition(
                        current,
                        status="paused",
                        actor="user",
                        reason_code="user_paused",
                        report="Goal paused by user.",
                    ),
                ),
            )
        if parsed.action == "resume":
            if current.status not in {"paused", "blocked"}:
                return CommandOutput(
                    output=f"Goal {current.goal_id} is {current.status}; it cannot be resumed.",
                    data={"error": "goal_not_resumable", "goal": current.to_public()},
                )
            token_budget = parsed.token_budget
            if token_budget is None:
                token_budget = current.token_budget
            if token_budget is not None and token_budget <= current.tokens_used:
                return CommandOutput(
                    output=(
                        "Resuming this Goal requires a new total token budget greater than "
                        f"tokens_used ({current.tokens_used})."
                    ),
                    data={"error": "token_budget_exhausted", "goal": current.to_public()},
                )
            return self._resume_output(current, token_budget=token_budget)
        if parsed.action == "cancel":
            if current.status == "active" and current.activation_id is not None:
                return CommandOutput(
                    output=f"Cancelling Goal {current.goal_id}.",
                    data={"goal_id": current.goal_id, "cancelled": True},
                    session_control_transitions=(
                        SessionControlTransition(
                            action="release",
                            controller_id="mode",
                            activation_id=current.activation_id,
                            payload={
                                "operation": "cancel",
                                "goal_id": current.goal_id,
                                "expected_revision": current.revision,
                            },
                            reason="user_cancelled",
                        ),
                    ),
                )
            with self._lock:
                latest = self._state.load(call.session_id).goal
                if latest is not None and latest.goal_id == current.goal_id:
                    self._state.delete(call.session_id)
            return CommandOutput(
                output=f"Cancelled Goal {current.goal_id}.",
                data={"goal_id": current.goal_id, "cancelled": True},
            )
        if parsed.action == "replace":
            assert parsed.source_request is not None
            return self._creation_request_output(
                source_request=parsed.source_request,
                token_budget=parsed.token_budget,
                replace_goal=current,
            )
        raise AssertionError(f"unsupported parsed Goal command: {parsed.action}")

    @staticmethod
    def _creation_request_output(
        *,
        source_request: str,
        token_budget: int | None,
        replace_goal: GoalRecord | None = None,
    ) -> CommandOutput:
        replace_goal_id = replace_goal.goal_id if replace_goal is not None else None
        operation = "replacement" if replace_goal_id is not None else "creation"
        return CommandOutput(
            output=f"Requesting Goal {operation} through the Agent.",
            data={
                "source_request": source_request,
                "token_budget": token_budget,
                "replace_goal_id": replace_goal_id,
            },
            session_inputs=(
                SessionInput(
                    prompt=goal_creation_request_prompt(
                        source_request=source_request,
                        token_budget=token_budget,
                        replace_goal_id=replace_goal_id,
                    ),
                    mode="follow_up",
                    title=f"Create Goal: {source_request[:48]}",
                ),
            ),
        )

    @staticmethod
    def _resume_output(
        goal: GoalRecord,
        *,
        token_budget: int | None,
    ) -> CommandOutput:
        return CommandOutput(
            output=f"Resuming Goal {goal.goal_id}.",
            data={
                "goal_id": goal.goal_id,
                "status": "active",
                "token_budget": token_budget,
            },
            session_inputs=(
                SessionInput(
                    prompt=continuation_prompt(goal),
                    mode="follow_up",
                    title=f"Resume Goal {goal.goal_id[:8]}",
                ),
            ),
            session_control_transitions=(
                SessionControlTransition(
                    action="acquire",
                    controller_id="mode",
                    payload={
                        "operation": "resume",
                        "goal_id": goal.goal_id,
                        "expected_goal_id": goal.goal_id,
                        "expected_revision": goal.revision,
                        "token_budget": token_budget,
                    },
                    reason="user_resumed_goal",
                ),
            ),
        )

    @staticmethod
    def _release_transition(
        goal: GoalRecord,
        *,
        status: GoalStatus,
        actor: GoalActor,
        reason_code: str,
        report: str,
    ) -> SessionControlTransition:
        assert goal.activation_id is not None
        return SessionControlTransition(
            action="release",
            controller_id="mode",
            activation_id=goal.activation_id,
            payload={
                "operation": "settle",
                "goal_id": goal.goal_id,
                "expected_revision": goal.revision,
                "status": status,
                "actor": actor,
                "reason_code": reason_code,
                "report": report,
            },
            reason=reason_code,
        )


def parse_goal_command(argument: str) -> ParsedGoalCommand:
    text = argument.strip()
    if not text or text.lower() == "status":
        return ParsedGoalCommand(action="status")
    head, _, tail = text.partition(" ")
    command = head.lower()
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
        budget, source_request, error = _parse_budget_prefix(tail.strip())
        if error is not None:
            return ParsedGoalCommand(action="replace", error=error)
        if not source_request:
            return ParsedGoalCommand(
                action="replace",
                error="Usage: /goal replace [--token-budget N] <request>",
            )
        return ParsedGoalCommand(
            action="replace",
            source_request=source_request,
            token_budget=budget,
        )
    budget, source_request, error = _parse_budget_prefix(text)
    if error is not None:
        return ParsedGoalCommand(action="create", error=error)
    if not source_request:
        return ParsedGoalCommand(action="create", error="Goal request cannot be empty.")
    return ParsedGoalCommand(
        action="create",
        source_request=source_request,
        token_budget=budget,
    )


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


def format_goal_status(goal: GoalRecord) -> str:
    budget = "unlimited" if goal.token_budget is None else str(goal.token_budget)
    remaining = (
        "unlimited"
        if goal.token_budget is None
        else str(max(goal.token_budget - goal.tokens_used, 0))
    )
    details = [
        f"Goal {goal.goal_id}: {goal.status} (actor: {goal.actor}).",
        f"Source request: {goal.source_request}",
        f"Outcome: {goal.contract.outcome}",
        _format_contract_list("Verification", goal.contract.verification),
        _format_contract_list("Constraints", goal.contract.constraints),
        _format_contract_list("Boundaries", goal.contract.boundaries),
        f"Iteration strategy: {goal.contract.iteration_strategy}",
        _format_contract_list("Blocked conditions", goal.contract.blocked_conditions),
        (
            f"Usage: {goal.tokens_used} tokens across {goal.runs_used} settled Runs; "
            f"budget: {budget}; remaining: {remaining}."
        ),
    ]
    if goal.reason_code is not None:
        details.append(f"Reason: {goal.reason_code}.")
    if goal.report is not None:
        details.append(f"Report: {goal.report}")
    return "\n".join(details)


def _format_contract_list(label: str, items: tuple[str, ...]) -> str:
    if not items:
        return f"{label}: none declared."
    return f"{label}:\n" + "\n".join(f"- {item}" for item in items)


__all__ = [
    "GoalCommandService",
    "ParsedGoalCommand",
    "format_goal_status",
    "parse_goal_command",
]
