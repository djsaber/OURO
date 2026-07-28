from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import replace
from uuid import uuid4

from ouro.sdk.plugins.v1 import (
    CommandContribution,
    ContextCall,
    ContextContribution,
    ContextFragment,
    ContextOutput,
    ContinuationDecision,
    ContinuationPolicyContribution,
    ContributionSet,
    PluginContext,
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
    goal_scaffold,
)
from .state import (
    UNFINISHED_GOAL_STATUSES,
    GoalActor,
    GoalRecord,
    GoalRunBinding,
    GoalStateRepository,
    GoalStatus,
    SessionGoalState,
    utc_now,
)

_TERMINAL_RUN_STATUSES = {"completed", "aborted", "failed", "paused", "recoverable"}


class GoalModePlugin:
    def __init__(self, context: PluginContext) -> None:
        required = {"sessions.write", "background.run"}
        missing = required - context.permissions
        if missing:
            raise RuntimeError(
                "Goal mode requires authorized permissions: " + ", ".join(sorted(missing))
            )
        self._state = GoalStateRepository(context.data)
        self._lock = threading.RLock()
        self._commands = GoalCommandService(self._state, self._lock)

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
                                "description": "The exact user-requested durable objective.",
                            },
                            "token_budget": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "Optional total token budget. Include only when the user "
                                    "explicitly supplied it."
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
                            "goal_id": {"type": "string", "minLength": 1},
                            "status": {"type": "string", "enum": ["complete", "blocked"]},
                            "report": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Evidence-based completion or blocker report.",
                            },
                        },
                        "required": ["goal_id", "status", "report"],
                        "additionalProperties": False,
                    },
                    permission_mode="auto",
                    permission_control="tool",
                    effect="state_changing",
                    batch_behavior="exclusive",
                    active_when="mode",
                ),
            ],
            commands=[
                CommandContribution(
                    id="goal",
                    description="Create and control a durable Goal for the current Session.",
                    usage=(
                        "[--token-budget N] <objective> | status | pause | "
                        "resume [--token-budget N] | cancel | "
                        "replace [--token-budget N] <objective>"
                    ),
                    handler=self._commands.handle,
                )
            ],
            session_controllers=[
                SessionControllerContribution(
                    id="mode",
                    description="Own one durable Goal lifecycle for a primary Session.",
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
            context_providers=[
                ContextContribution(
                    id="goal-scaffold",
                    handler=self.goal_context,
                    priority=10,
                    timeout_seconds=5,
                    failure_mode="block",
                    active_when="mode",
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
        )

    async def start(self) -> None:
        return None

    async def stop(self, reason, deadline) -> None:
        del reason, deadline

    def get_goal(self, call: ToolCall) -> ToolOutput:
        with self._lock:
            goal = self._state.load(call.session_id).goal
        if goal is None:
            return ToolOutput(
                content="No Goal exists for this Session.",
                structured={"goal": None},
            )
        return ToolOutput(
            content=format_goal_status(goal),
            structured={"goal": goal.to_public()},
        )

    def create_goal(self, call: ToolCall) -> ToolOutput:
        objective = _non_empty_argument(call.arguments.get("objective"))
        if objective is None:
            return _tool_error("invalid_objective", "CreateGoal requires a non-empty objective.")
        token_budget, budget_error = _optional_budget(call.arguments.get("token_budget"))
        if budget_error is not None:
            return _tool_error("invalid_token_budget", budget_error)
        with self._lock:
            current = self._state.load(call.session_id).goal
        if current is not None and current.status in UNFINISHED_GOAL_STATUSES:
            return _tool_error(
                "unfinished_goal_exists",
                (
                    f"Goal {current.goal_id} is {current.status}. Finish it or ask the user "
                    "to pause/cancel/replace it before creating another Goal."
                ),
            )
        goal_id = uuid4().hex
        return ToolOutput(
            content=f"Goal {goal_id} will be activated for: {objective}",
            structured={
                "goal_id": goal_id,
                "objective": objective,
                "status": "active",
                "token_budget": token_budget,
            },
            session_control_transitions=(
                SessionControlTransition(
                    action="acquire",
                    controller_id="mode",
                    payload={
                        "operation": "create",
                        "goal_id": goal_id,
                        "objective": objective,
                        "token_budget": token_budget,
                        "actor": "model",
                    },
                    reason="model_created_goal",
                ),
            ),
        )

    def update_goal(self, call: ToolCall) -> ToolOutput:
        goal_id = _non_empty_argument(call.arguments.get("goal_id"))
        status = call.arguments.get("status")
        report = _non_empty_argument(call.arguments.get("report"))
        if goal_id is None:
            return _tool_error("invalid_goal_id", "UpdateGoal requires the exact goal_id.")
        if status not in {"complete", "blocked"}:
            return _tool_error("invalid_status", "UpdateGoal status must be complete or blocked.")
        if report is None:
            return _tool_error("invalid_report", "UpdateGoal requires a non-empty report.")
        with self._lock:
            goal = self._state.load(call.session_id).goal
        if goal is None:
            return _tool_error("goal_not_found", "No Goal exists for this Session.")
        if goal.goal_id != goal_id:
            return _tool_error(
                "stale_goal_id",
                f"Goal identity is stale. The current goal_id is {goal.goal_id}.",
            )
        if goal.status != "active" or goal.activation_id is None:
            return _tool_error(
                "goal_not_active", f"Goal {goal.goal_id} is {goal.status}, not active."
            )
        terminal_status: GoalStatus = status
        return ToolOutput(
            content=(
                f"Goal {goal.goal_id} marked {terminal_status}. Goal mode will exit after "
                "this tool result is committed."
            ),
            structured={
                "goal_id": goal.goal_id,
                "status": terminal_status,
                "report": report,
            },
            session_control_transitions=(
                SessionControlTransition(
                    action="release",
                    controller_id="mode",
                    activation_id=goal.activation_id,
                    payload={
                        "operation": "settle",
                        "goal_id": goal.goal_id,
                        "expected_revision": goal.revision,
                        "status": terminal_status,
                        "actor": "model",
                        "reason_code": (
                            "goal_completed" if terminal_status == "complete" else "goal_blocked"
                        ),
                        "report": report,
                    },
                    reason=f"goal_{terminal_status}",
                ),
            ),
        )

    def activate_goal_controller(self, call: SessionControllerCall) -> SessionControllerOutput:
        if call.session_kind != "primary":
            raise RuntimeError("Goal mode is only supported for primary Sessions")
        payload = _mapping_payload(call.payload)
        operation = _required_payload_text(payload, "operation")
        with self._lock:
            state = self._state.load(call.session_id)
            if operation in {"create", "replace"}:
                goal_id = _required_payload_text(payload, "goal_id")
                objective = _required_payload_text(payload, "objective")
                token_budget = _payload_budget(payload.get("token_budget"))
                actor = _payload_actor(payload.get("actor"))
                if (
                    state.goal is not None
                    and state.goal.status == "active"
                    and state.goal.activation_id == call.activation_id
                    and state.goal.goal_id == goal_id
                ):
                    goal = state.goal
                else:
                    if operation == "create" and (
                        state.goal is not None
                        and state.goal.status in UNFINISHED_GOAL_STATUSES
                    ):
                        raise RuntimeError("an unfinished Goal already exists")
                    if operation == "replace":
                        _validate_expected_goal(payload, state.goal)
                    goal = _new_goal(
                        goal_id=goal_id,
                        objective=objective,
                        token_budget=token_budget,
                        actor=actor,
                        activation_id=call.activation_id,
                    )
                    self._state.save(call.session_id, SessionGoalState(goal=goal))
            elif operation == "resume":
                resume_goal = state.goal
                if resume_goal is None:
                    raise RuntimeError("cannot resume a missing Goal")
                goal = resume_goal
                if (
                    goal.status == "active"
                    and goal.activation_id == call.activation_id
                    and goal.goal_id == payload.get("goal_id")
                ):
                    pass
                else:
                    _validate_expected_goal(payload, goal)
                    if goal.status not in {"paused", "blocked"}:
                        raise RuntimeError(f"Goal cannot resume from status {goal.status}")
                    token_budget = _payload_budget(payload.get("token_budget"))
                    if token_budget is not None and token_budget <= goal.tokens_used:
                        raise RuntimeError(
                            "resumed token budget must be greater than tokens already used"
                        )
                    stamp = utc_now()
                    goal = replace(
                        goal,
                        status="active",
                        actor="user",
                        revision=goal.revision + 1,
                        activation_id=call.activation_id,
                        token_budget=token_budget,
                        reason_code=None,
                        report=None,
                        updated_at=stamp,
                        status_changed_at=stamp,
                    )
                    self._state.save(call.session_id, SessionGoalState(goal=goal))
            else:
                raise RuntimeError(f"unsupported Goal activation operation: {operation}")
        return SessionControllerOutput(
            output=f"Goal {goal.goal_id} activated.",
            data={"goal": goal.to_public()},
            owner_state_revision=goal.revision,
        )

    def deactivate_goal_controller(self, call: SessionControllerCall) -> SessionControllerOutput:
        payload = _mapping_payload(call.payload, allow_empty=True)
        with self._lock:
            state = self._state.load(call.session_id)
            goal = state.goal
            if goal is None:
                return SessionControllerOutput(output="No Goal remains active.")
            if goal.activation_id != call.activation_id:
                return SessionControllerOutput(
                    output="Superseded Goal activation required no cleanup.",
                    data={"goal": goal.to_public(), "superseded": True},
                    owner_state_revision=goal.revision,
                )
            operation = payload.get("operation")
            if operation == "cancel":
                _validate_expected_goal(payload, goal)
                self._state.delete(call.session_id)
                return SessionControllerOutput(
                    output=f"Goal {goal.goal_id} cancelled.",
                    data={"goal_id": goal.goal_id, "cancelled": True},
                    owner_state_revision=goal.revision + 1,
                )
            if operation == "settle":
                _validate_expected_goal(payload, goal)
                status = _payload_status(payload.get("status"))
                actor = _payload_actor(payload.get("actor"))
                reason_code = _required_payload_text(payload, "reason_code")
                report = _required_payload_text(payload, "report")
            else:
                status = "paused"
                actor = "user" if (call.reason or "").startswith("user_") else "runtime"
                reason_code = call.reason or "controller_released"
                report = "Goal mode was released before the Goal reached a terminal result."
            stamp = utc_now()
            settled = replace(
                goal,
                status=status,
                actor=actor,
                revision=goal.revision + 1,
                activation_id=None,
                reason_code=reason_code,
                report=report,
                updated_at=stamp,
                status_changed_at=stamp,
            )
            self._state.save(
                call.session_id,
                SessionGoalState(goal=settled, run_binding=state.run_binding),
            )
        return SessionControllerOutput(
            output=f"Goal {settled.goal_id} is now {settled.status}.",
            data={"goal": settled.to_public()},
            owner_state_revision=settled.revision,
        )

    def goal_context(self, call: ContextCall) -> ContextOutput:
        with self._lock:
            state = self._state.load(call.session_id)
            goal = state.goal
            if goal is None or goal.status != "active" or goal.activation_id is None:
                raise RuntimeError("Goal controller is active without a matching active Goal")
            binding = GoalRunBinding(
                run_id=call.run_id,
                goal_id=goal.goal_id,
                goal_revision=goal.revision,
                activation_id=goal.activation_id,
            )
            if state.run_binding != binding:
                self._state.save(
                    call.session_id,
                    SessionGoalState(goal=goal, run_binding=binding),
                )
        return ContextOutput(
            fragments=(ContextFragment(text=goal_scaffold(goal), persistence="message"),)
        )

    def goal_continuation(self, outcome: RunOutcome) -> ContinuationDecision:
        if outcome.status not in _TERMINAL_RUN_STATUSES:
            return ContinuationDecision()
        with self._lock:
            state = self._state.load(outcome.session_id)
            goal = state.goal
            binding = state.run_binding
            if (
                goal is None
                or binding is None
                or binding.run_id != outcome.run_id
                or binding.goal_id != goal.goal_id
            ):
                return ContinuationDecision()
            if goal.status == "active" and goal.activation_id != binding.activation_id:
                return ContinuationDecision()
            usage_tokens = _canonical_usage_tokens(outcome.usage)
            if goal.last_accounted_run_id != outcome.run_id:
                stamp = utc_now()
                goal = replace(
                    goal,
                    revision=goal.revision + 1,
                    tokens_used=goal.tokens_used + (usage_tokens or 0),
                    runs_used=goal.runs_used + 1,
                    last_accounted_run_id=outcome.run_id,
                    updated_at=stamp,
                )
                self._state.save(
                    outcome.session_id,
                    SessionGoalState(goal=goal, run_binding=binding),
                )

            if goal.status == "complete":
                return ContinuationDecision(
                    action="complete",
                    reason=goal.reason_code or "goal_completed",
                    state_revision=goal.revision,
                )
            if goal.status in {"paused", "blocked"}:
                return ContinuationDecision(
                    action="pause",
                    reason=goal.reason_code or f"goal_{goal.status}",
                    state_revision=goal.revision,
                )
            assert goal.activation_id is not None
            if outcome.status != "completed":
                return self._runtime_pause_decision(
                    goal,
                    binding,
                    reason_code=f"run_{outcome.status}",
                    report=outcome.error or f"Run ended with status {outcome.status}.",
                )
            if goal.token_budget is not None and usage_tokens is None:
                return self._runtime_pause_decision(
                    goal,
                    binding,
                    reason_code="usage_unavailable",
                    report=(
                        "Canonical provider token usage was unavailable, so the budgeted "
                        "Goal was paused instead of treating missing usage as zero."
                    ),
                )
            if goal.token_budget is not None and goal.tokens_used >= goal.token_budget:
                return self._runtime_pause_decision(
                    goal,
                    binding,
                    status="blocked",
                    reason_code="token_budget_exhausted",
                    report=(
                        f"Token budget exhausted: used {goal.tokens_used} of "
                        f"{goal.token_budget} tokens."
                    ),
                )
            return ContinuationDecision(
                action="continue",
                prompt=continuation_prompt(goal),
                title=f"Continue Goal {goal.goal_id[:8]}",
                reason="goal_still_active",
                state_revision=goal.revision,
            )

    def _runtime_pause_decision(
        self,
        goal: GoalRecord,
        binding: GoalRunBinding,
        *,
        reason_code: str,
        report: str,
        status: GoalStatus = "paused",
    ) -> ContinuationDecision:
        return ContinuationDecision(
            action="pause",
            reason=reason_code,
            state_revision=goal.revision,
            session_control_transitions=(
                SessionControlTransition(
                    action="release",
                    controller_id="mode",
                    activation_id=binding.activation_id,
                    payload={
                        "operation": "settle",
                        "goal_id": goal.goal_id,
                        "expected_revision": goal.revision,
                        "status": status,
                        "actor": "runtime",
                        "reason_code": reason_code,
                        "report": report,
                    },
                    reason=reason_code,
                ),
            ),
        )


def _new_goal(
    *,
    goal_id: str,
    objective: str,
    token_budget: int | None,
    actor: GoalActor,
    activation_id: str,
) -> GoalRecord:
    stamp = utc_now()
    return GoalRecord(
        goal_id=goal_id,
        objective=objective,
        status="active",
        actor=actor,
        revision=1,
        activation_id=activation_id,
        token_budget=token_budget,
        tokens_used=0,
        runs_used=0,
        last_accounted_run_id=None,
        reason_code=None,
        report=None,
        created_at=stamp,
        updated_at=stamp,
        status_changed_at=stamp,
    )


def _tool_error(error_type: str, message: str) -> ToolOutput:
    return ToolOutput(
        content=message,
        structured={"error_type": error_type, "message": message},
        is_error=True,
    )


def _non_empty_argument(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


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
    raise RuntimeError("Goal controller payload must be an object")


def _required_payload_text(payload: Mapping[str, object], name: str) -> str:
    value = _non_empty_argument(payload.get(name))
    if value is None:
        raise RuntimeError(f"Goal controller payload requires {name}")
    return value


def _payload_budget(value: object) -> int | None:
    budget, error = _optional_budget(value)
    if error is not None:
        raise RuntimeError(error)
    return budget


def _payload_actor(value: object) -> GoalActor:
    if value not in {"user", "model", "runtime"}:
        raise RuntimeError("Goal controller payload has an invalid actor")
    return value


def _payload_status(value: object) -> GoalStatus:
    if value not in {"paused", "blocked", "complete"}:
        raise RuntimeError("Goal controller payload has an invalid terminal status")
    return value


def _validate_expected_goal(
    payload: Mapping[str, object],
    goal: GoalRecord | None,
) -> None:
    if goal is None:
        raise RuntimeError("Goal controller expected an existing Goal")
    expected_id = payload.get("expected_goal_id", payload.get("goal_id"))
    expected_revision = payload.get("expected_revision")
    if expected_id != goal.goal_id:
        raise RuntimeError("Goal controller payload has a stale goal identity")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision != goal.revision
    ):
        raise RuntimeError("Goal controller payload has a stale Goal revision")


def _canonical_usage_tokens(usage: Mapping[str, int | float]) -> int | None:
    if "input_tokens" not in usage or "output_tokens" not in usage:
        return None
    values = (usage["input_tokens"], usage["output_tokens"])
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or not float(value).is_integer()
        for value in values
    ):
        return None
    return sum(int(value) for value in values)


def create_plugin(context: PluginContext) -> GoalModePlugin:
    return GoalModePlugin(context)


__all__ = ["GoalModePlugin", "create_plugin"]
