from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from uuid import uuid4

from ouro.sdk.plugins.v3 import (
    CommandContribution,
    CommandOutput,
    ContinuationDecision,
    ContinuationPolicyContribution,
    ContributionSet,
    JsonValue,
    PluginContext,
    PluginSessionControl,
    PromptPolicyCall,
    PromptPolicyContribution,
    PromptPolicyDecision,
    RunOutcome,
    SessionControlClaim,
    SessionControllerCall,
    SessionControllerContribution,
    SessionControllerOutput,
    SessionControlTransition,
    ToolCall,
    ToolContribution,
    ToolOutput,
)

from .commands import GoalCommandService, format_goal_status
from .prompts import (
    CREATE_GOAL_DESCRIPTION,
    GET_GOAL_DESCRIPTION,
    UPDATE_GOAL_DESCRIPTION,
    continuation_prompt,
)
from .state import (
    GoalRecord,
    GoalStateRepository,
    GoalStatus,
    SessionGoalState,
    utc_now,
)

_TERMINAL_RUN_STATUSES = {"completed", "aborted", "failed", "paused", "recoverable"}


class GoalPlugin:
    def __init__(self, context: PluginContext) -> None:
        required = {"sessions.write", "background.run", "sessions.prompt.inspect"}
        missing = required - context.permissions
        if missing:
            raise RuntimeError(
                "Goal requires authorized permissions: " + ", ".join(sorted(missing))
            )

    async def prepare(self) -> ContributionSet:
        return ContributionSet(
            tools=[
                ToolContribution(
                    id="get-goal",
                    exposed_name="GetGoal",
                    description=GET_GOAL_DESCRIPTION,
                    handler=self.get_goal,
                    parameters_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    permission_mode="auto",
                    permission_control="tool",
                    effect="read_only",
                ),
                ToolContribution(
                    id="create-goal",
                    exposed_name="CreateGoal",
                    description=CREATE_GOAL_DESCRIPTION,
                    handler=self.create_goal,
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "objective": {
                                "type": "string",
                                "minLength": 1,
                                "description": "The exact requested objective.",
                            },
                            "token_budget": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "The exact total token budget when one was explicitly "
                                    "requested."
                                ),
                            },
                        },
                        "required": ["objective"],
                        "additionalProperties": False,
                    },
                    permission_mode="approval",
                    permission_control="host",
                    effect="state_changing",
                    batch_behavior="exclusive",
                ),
                ToolContribution(
                    id="update-goal",
                    exposed_name="UpdateGoal",
                    description=UPDATE_GOAL_DESCRIPTION,
                    handler=self.update_goal,
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "enum": ["complete", "blocked"]},
                            "report": {
                                "type": "string",
                                "minLength": 1,
                                "description": "The result and evidence, or the exact blocker.",
                            },
                        },
                        "required": ["status", "report"],
                        "additionalProperties": False,
                    },
                    permission_mode="auto",
                    permission_control="tool",
                    effect="state_changing",
                    batch_behavior="exclusive",
                ),
            ],
            commands=[
                CommandContribution(
                    id="goal",
                    description="Create and control the latest goal for this conversation.",
                    usage=(
                        "[--token-budget N] <objective> | status | pause | "
                        "resume [--token-budget N] | cancel | "
                        "replace [--token-budget N] <objective>"
                    ),
                    handler=self.goal_command,
                )
            ],
            session_controllers=[
                SessionControllerContribution(
                    id="mode",
                    description="Own the latest Goal lifecycle.",
                    claims=(
                        SessionControlClaim(
                            domain="ouro.session.primary_behavior",
                            access="exclusive",
                        ),
                    ),
                    activate=self.activate_goal_controller,
                    deactivate=self.deactivate_goal_controller,
                )
            ],
            continuation_policies=[
                ContinuationPolicyContribution(
                    id="goal-continuation",
                    handler=self.goal_continuation,
                    priority=10,
                    timeout_seconds=5,
                    active_when="mode",
                )
            ],
            prompt_policies=[
                PromptPolicyContribution(
                    id="goal-input-gate",
                    handler=self.goal_input_gate,
                    priority=10,
                    timeout_seconds=5,
                    active_when="mode",
                )
            ],
        )

    async def start(self) -> None:
        return None

    async def stop(self, reason, deadline) -> None:
        del reason, deadline

    def goal_command(self, call):
        if call.session_id is None:
            return CommandOutput(output="The /goal command requires a target conversation.")
        return GoalCommandService(GoalStateRepository(call.session_state)).handle(call)

    def get_goal(self, call: ToolCall) -> ToolOutput:
        repository = GoalStateRepository(call.session_state)
        goal = repository.load().goal
        if goal is None:
            return _tool_error("goal_not_found", "No goal exists.")
        return ToolOutput(
            content=format_goal_status(goal, tokens_used=repository.tokens_used(goal)),
            structured={"goal": goal.to_public(tokens_used=repository.tokens_used(goal))},
        )

    def create_goal(self, call: ToolCall) -> ToolOutput:
        repository = GoalStateRepository(call.session_state)
        objective = _non_empty_text(call.arguments.get("objective"))
        if objective is None:
            return _tool_error("invalid_objective", "CreateGoal requires an objective.")
        token_budget, budget_error = _optional_budget(call.arguments.get("token_budget"))
        if budget_error is not None:
            return _tool_error("invalid_token_budget", budget_error)
        current = repository.load().goal
        if current is not None and current.status != "complete":
            return _tool_error(
                "unfinished_goal_exists",
                f"The current goal is {current.status}; it must be updated by the user first.",
            )
        return ToolOutput(
            content="The goal will be created.",
            structured={
                "goal": {
                    "objective": objective,
                    "status": "active",
                    "token_budget": token_budget,
                    "tokens_used": 0,
                }
            },
            session_control_transitions=(
                SessionControlTransition(
                    action="acquire",
                    controller_id="mode",
                    payload={
                        "operation": "create",
                        "objective": objective,
                        "token_budget": token_budget,
                    },
                    reason="goal_created",
                ),
            ),
        )

    def update_goal(self, call: ToolCall) -> ToolOutput:
        repository = GoalStateRepository(call.session_state)
        status = call.arguments.get("status")
        report = _non_empty_text(call.arguments.get("report"))
        if status not in {"complete", "blocked"}:
            return _tool_error("invalid_status", "Status must be complete or blocked.")
        if report is None:
            return _tool_error("invalid_report", "UpdateGoal requires a non-empty report.")
        goal = repository.load().goal
        control = repository.control()
        binding = repository.run_control()
        if goal is None or goal.status != "active" or control is None:
            return _tool_error("goal_not_active", "There is no active goal to update.")
        if (
            binding is None
            or binding.activation_id != control.activation_id
            or binding.usage_scope != goal.goal_id
        ):
            return _tool_error(
                "stale_goal_update",
                "This work started before the current goal was created or updated.",
            )
        payload: dict[str, JsonValue] = {"operation": status, "report": report}
        if status == "complete":
            transition = SessionControlTransition(
                action="release",
                controller_id="mode",
                activation_id=control.activation_id,
                payload=payload,
                reason="goal_complete",
            )
        else:
            transition = SessionControlTransition(
                action="replace",
                controller_id="mode",
                replace_activation_ids=(control.activation_id,),
                payload=payload,
                reason="goal_blocked",
            )
        return ToolOutput(
            content=f"The current goal will be marked {status}.",
            structured={"status": status, "report": report},
            session_control_transitions=(transition,),
        )

    def activate_goal_controller(self, call: SessionControllerCall) -> SessionControllerOutput:
        repository = GoalStateRepository(call.session_state)
        if call.session_kind != "primary":
            raise RuntimeError("Goal is only supported for primary conversations")
        payload = _mapping_payload(call.payload)
        operation = _required_payload_text(payload, "operation")
        current = repository.load().goal
        stamp = utc_now()
        if operation == "create":
            if current is not None and current.status != "complete":
                raise RuntimeError("an unfinished Goal already exists")
            goal = GoalRecord(
                goal_id=f"goal_{uuid4().hex}",
                objective=_required_payload_text(payload, "objective"),
                status="active",
                token_budget=_payload_budget(payload.get("token_budget")),
                created_at=stamp,
                updated_at=stamp,
                report=None,
            )
        else:
            if current is None or current.status == "complete":
                raise RuntimeError("Goal transition requires an unfinished Goal")
            goal = replace(current, updated_at=stamp)
            if operation == "pause":
                if goal.status != "active":
                    raise RuntimeError(f"Goal cannot pause from status {goal.status}")
                goal = replace(goal, status="paused", report=None)
            elif operation in {"resume", "replace"}:
                if operation == "resume" and goal.status not in {
                    "paused",
                    "blocked",
                    "budget_exhausted",
                }:
                    raise RuntimeError(f"Goal cannot resume from status {goal.status}")
                token_budget = _payload_budget(payload.get("token_budget"))
                _require_budget_above_usage(token_budget, repository.tokens_used(goal))
                goal = replace(
                    goal,
                    objective=(
                        _required_payload_text(payload, "objective")
                        if operation == "replace"
                        else goal.objective
                    ),
                    status="active",
                    token_budget=token_budget,
                    report=None,
                )
            elif operation in {"blocked", "budget_exhausted", "runtime_paused"}:
                if goal.status != "active":
                    raise RuntimeError(f"Goal cannot stop from status {goal.status}")
                stopped_status: GoalStatus = (
                    "paused" if operation == "runtime_paused" else cast(GoalStatus, operation)
                )
                goal = replace(
                    goal,
                    status=stopped_status,
                    report=_required_payload_text(payload, "report"),
                )
            else:
                raise RuntimeError(f"unsupported Goal transition: {operation}")
        repository.save(SessionGoalState(goal=goal))
        return SessionControllerOutput(
            output=f"Goal is now {goal.status}.",
            usage_scope=goal.goal_id if goal.status == "active" else None,
        )

    def deactivate_goal_controller(self, call: SessionControllerCall) -> SessionControllerOutput:
        repository = GoalStateRepository(call.session_state)
        goal = repository.load().goal
        if goal is None:
            return SessionControllerOutput(output="No Goal remains.")
        # The successor callback uses the same unpublished draft for lifecycle changes.
        if call.replacement_controller_id == call.controller_id:
            return SessionControllerOutput()
        payload = (
            _mapping_payload(call.payload, allow_empty=True)
            if call.replacement_controller_id is None
            else {}
        )
        if payload.get("operation") == "complete":
            completed = replace(
                goal,
                status="complete",
                report=_required_payload_text(payload, "report"),
                updated_at=utc_now(),
            )
            repository.save(SessionGoalState(goal=completed))
            return SessionControllerOutput(
                output="Goal completed.",
            )
        repository.delete()
        return SessionControllerOutput(
            output="Goal cancelled because its control was released.",
            data={"goal": None, "cancelled": True},
        )

    def goal_input_gate(self, call: PromptPolicyCall) -> PromptPolicyDecision:
        repository = GoalStateRepository(call.session_state)
        goal = repository.load().goal
        if goal is None or goal.status in {"active", "complete"}:
            return PromptPolicyDecision()
        return PromptPolicyDecision(
            action="deny",
            reason=(
                f"The current goal is {goal.status}. Use /goal resume, /goal replace, "
                "or /goal cancel before sending ordinary input."
            ),
        )

    def goal_continuation(self, outcome: RunOutcome) -> ContinuationDecision:
        repository = GoalStateRepository(outcome.session_state)
        if outcome.status not in _TERMINAL_RUN_STATUSES:
            return ContinuationDecision()
        goal = repository.load().goal
        control = repository.control()
        binding = repository.run_control()
        if goal is None or control is None or binding is None:
            return ContinuationDecision()
        if goal.status in {"paused", "blocked", "budget_exhausted"}:
            return ContinuationDecision(action="pause", reason=f"goal_{goal.status}")
        if (
            goal.status != "active"
            or binding.activation_id != control.activation_id
            or binding.usage_scope != goal.goal_id
        ):
            return ContinuationDecision()
        if outcome.status != "completed":
            return self._runtime_stop_decision(
                control,
                operation="runtime_paused",
                reason=f"run_{outcome.status}",
                report=outcome.error or f"Work ended with status {outcome.status}.",
            )
        tokens_used = repository.tokens_used(goal)
        if goal.token_budget is not None and (
            tokens_used is None
            or "input_tokens" not in outcome.usage
            or "output_tokens" not in outcome.usage
        ):
            return self._runtime_stop_decision(
                control,
                operation="runtime_paused",
                reason="usage_unavailable",
                report="Token usage was unavailable, so budgeted work was paused.",
            )
        if (
            goal.token_budget is not None
            and tokens_used is not None
            and tokens_used >= goal.token_budget
        ):
            return self._runtime_stop_decision(
                control,
                operation="budget_exhausted",
                reason="goal_budget_exhausted",
                report=(
                    f"Token budget exhausted: used {tokens_used} of {goal.token_budget} tokens."
                ),
            )
        return ContinuationDecision(
            action="continue",
            prompt=continuation_prompt(goal, tokens_used=tokens_used),
            title="Continue Goal",
            role="user",
            scaffold=True,
            semantic_tag="goal-continue",
            coalesce_key="goal-continuation",
            reason="goal_active",
        )

    @staticmethod
    def _runtime_stop_decision(
        control: PluginSessionControl,
        *,
        operation: str,
        reason: str,
        report: str,
    ) -> ContinuationDecision:
        return ContinuationDecision(
            action="pause",
            reason=reason,
            session_control_transitions=(
                SessionControlTransition(
                    action="replace",
                    controller_id="mode",
                    replace_activation_ids=(control.activation_id,),
                    payload={"operation": operation, "report": report},
                    reason=reason,
                ),
            ),
        )


def _tool_error(error_type: str, message: str) -> ToolOutput:
    return ToolOutput(
        content=message,
        structured={"error_type": error_type, "message": message},
        is_error=True,
    )


def _non_empty_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_budget(value: object) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None, "Token budget must be a positive integer."
    return value, None


def _mapping_payload(value: object, *, allow_empty: bool = False) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if value is None and allow_empty:
        return {}
    raise RuntimeError("Goal transition data must be an object")


def _required_payload_text(payload: Mapping[str, object], name: str) -> str:
    value = _non_empty_text(payload.get(name))
    if value is None:
        raise RuntimeError(f"Goal transition requires {name}")
    return value


def _payload_budget(value: object) -> int | None:
    budget, error = _optional_budget(value)
    if error is not None:
        raise RuntimeError(error)
    return budget


def _require_budget_above_usage(token_budget: int | None, tokens_used: int | None) -> None:
    if token_budget is None:
        return
    if tokens_used is None:
        raise RuntimeError("Token usage is unavailable; a total budget cannot be validated")
    if token_budget <= tokens_used:
        raise RuntimeError("total token budget must be greater than tokens already used")


def create_plugin(context: PluginContext) -> GoalPlugin:
    return GoalPlugin(context)


__all__ = ["GoalPlugin", "create_plugin"]
