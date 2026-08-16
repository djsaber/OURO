from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import replace

from ouro.sdk.plugins.v1 import (
    CommandContribution,
    ContextCall,
    ContextContribution,
    ContextOutput,
    ContinuationDecision,
    ContinuationPolicyContribution,
    ContributionSet,
    JsonValue,
    PluginContext,
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
    GoalRunBinding,
    GoalStateRepository,
    GoalStatus,
    SessionGoalState,
    utc_now,
)

_TERMINAL_RUN_STATUSES = {"completed", "aborted", "failed", "paused", "recoverable"}


class GoalModePlugin:
    def __init__(self, context: PluginContext) -> None:
        required = {"sessions.write", "background.run", "sessions.prompt.inspect"}
        missing = required - context.permissions
        if missing:
            raise RuntimeError(
                "Goal mode requires authorized permissions: " + ", ".join(sorted(missing))
            )
        if context.session_state is None:
            raise RuntimeError("Goal mode requires conversation state storage")
        self._state = GoalStateRepository(context.session_state)
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
                    handler=self._commands.handle,
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
            context_providers=[
                ContextContribution(
                    id="goal-run-binding",
                    handler=self.bind_goal_run,
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

    def get_goal(self, call: ToolCall) -> ToolOutput:
        with self._lock:
            goal = self._state.load(call.session_id).goal
        if goal is None:
            return _tool_error("goal_not_found", "No goal exists.")
        return ToolOutput(
            content=format_goal_status(goal),
            structured={"goal": goal.to_public()},
        )

    def create_goal(self, call: ToolCall) -> ToolOutput:
        objective = _non_empty_text(call.arguments.get("objective"))
        if objective is None:
            return _tool_error("invalid_objective", "CreateGoal requires an objective.")
        token_budget, budget_error = _optional_budget(call.arguments.get("token_budget"))
        if budget_error is not None:
            return _tool_error("invalid_token_budget", budget_error)
        with self._lock:
            current = self._state.load(call.session_id).goal
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
                        "source_run_id": call.run_id,
                    },
                    reason="goal_created",
                ),
            ),
        )

    def update_goal(self, call: ToolCall) -> ToolOutput:
        status = call.arguments.get("status")
        report = _non_empty_text(call.arguments.get("report"))
        if status not in {"complete", "blocked"}:
            return _tool_error("invalid_status", "Status must be complete or blocked.")
        if report is None:
            return _tool_error("invalid_report", "UpdateGoal requires a non-empty report.")
        with self._lock:
            state = self._state.load(call.session_id)
        goal = state.goal
        binding = state.run_binding
        if goal is None or goal.status != "active" or goal.activation_id is None:
            return _tool_error("goal_not_active", "There is no active goal to update.")
        if (
            binding is None
            or binding.run_id != call.run_id
            or binding.activation_id != goal.activation_id
            or binding.goal_revision != goal.revision
        ):
            return _tool_error(
                "stale_goal_update",
                "This work started before the current goal was created or updated.",
            )
        payload: dict[str, JsonValue] = {
            "operation": status,
            "expected_activation_id": goal.activation_id,
            "expected_revision": goal.revision,
            "report": report,
            "source_run_id": call.run_id,
        }
        if status == "complete":
            transition = SessionControlTransition(
                action="release",
                controller_id="mode",
                activation_id=goal.activation_id,
                payload=payload,
                reason="goal_complete",
            )
        else:
            transition = SessionControlTransition(
                action="replace",
                controller_id="mode",
                replace_activation_ids=(goal.activation_id,),
                payload=payload,
                reason="goal_blocked",
            )
        return ToolOutput(
            content=f"The current goal will be marked {status}.",
            structured={"status": status, "report": report},
            session_control_transitions=(transition,),
        )

    def activate_goal_controller(self, call: SessionControllerCall) -> SessionControllerOutput:
        if call.session_kind != "primary":
            raise RuntimeError("Goal mode is only supported for primary conversations")
        payload = _mapping_payload(call.payload)
        operation = _required_payload_text(payload, "operation")
        with self._lock:
            state = self._state.load(call.session_id)
            if operation == "create":
                if (
                    state.goal is not None
                    and state.goal.activation_id == call.activation_id
                    and state.goal.status == "active"
                ):
                    goal = state.goal
                    binding = state.run_binding
                else:
                    if state.goal is not None and state.goal.status != "complete":
                        raise RuntimeError("an unfinished Goal already exists")
                    objective = _required_payload_text(payload, "objective")
                    token_budget = _payload_budget(payload.get("token_budget"))
                    stamp = utc_now()
                    goal = GoalRecord(
                        objective=objective,
                        status="active",
                        token_budget=token_budget,
                        tokens_used=0,
                        created_at=stamp,
                        updated_at=stamp,
                        report=None,
                        activation_id=call.activation_id,
                        revision=1,
                        last_accounted_run_id=None,
                    )
                    source_run_id = _optional_payload_text(payload.get("source_run_id"))
                    binding = (
                        GoalRunBinding(
                            run_id=source_run_id,
                            activation_id=call.activation_id,
                            goal_revision=goal.revision,
                        )
                        if source_run_id is not None
                        else None
                    )
                    self._state.save(
                        call.session_id,
                        SessionGoalState(goal=goal, run_binding=binding),
                    )
            else:
                current_goal = state.goal
                if current_goal is None or current_goal.activation_id is None:
                    raise RuntimeError("Goal transition requires an unfinished Goal")
                _validate_expected_goal(payload, current_goal)
                stamp = utc_now()
                goal = replace(
                    current_goal,
                    activation_id=call.activation_id,
                    revision=current_goal.revision + 1,
                    updated_at=stamp,
                )
                if operation == "pause":
                    if goal.status != "active":
                        raise RuntimeError(f"Goal cannot pause from status {goal.status}")
                    goal = replace(goal, status="paused", report=None)
                elif operation == "resume":
                    if goal.status not in {"paused", "blocked", "budget_exhausted"}:
                        raise RuntimeError(f"Goal cannot resume from status {goal.status}")
                    token_budget = _payload_budget(payload.get("token_budget"))
                    _require_budget_above_usage(token_budget, goal.tokens_used)
                    goal = replace(
                        goal,
                        status="active",
                        token_budget=token_budget,
                        report=None,
                    )
                elif operation == "replace":
                    if goal.status == "complete":
                        raise RuntimeError("completed Goal cannot be replaced")
                    token_budget = _payload_budget(payload.get("token_budget"))
                    _require_budget_above_usage(token_budget, goal.tokens_used)
                    goal = replace(
                        goal,
                        objective=_required_payload_text(payload, "objective"),
                        status="active",
                        token_budget=token_budget,
                        report=None,
                    )
                elif operation == "blocked":
                    if goal.status != "active":
                        raise RuntimeError(f"Goal cannot be blocked from status {goal.status}")
                    goal = replace(
                        goal,
                        status="blocked",
                        report=_required_payload_text(payload, "report"),
                    )
                elif operation in {"budget_exhausted", "runtime_paused"}:
                    if goal.status != "active":
                        raise RuntimeError(f"Goal cannot stop from status {goal.status}")
                    stopped_status: GoalStatus = (
                        "budget_exhausted" if operation == "budget_exhausted" else "paused"
                    )
                    goal = replace(
                        goal,
                        status=stopped_status,
                        report=_required_payload_text(payload, "report"),
                    )
                else:
                    raise RuntimeError(f"unsupported Goal transition: {operation}")
                source_run_id = _optional_payload_text(payload.get("source_run_id"))
                binding = (
                    GoalRunBinding(
                        run_id=source_run_id,
                        activation_id=call.activation_id,
                        goal_revision=goal.revision,
                    )
                    if source_run_id is not None
                    else state.run_binding
                )
                self._state.save(
                    call.session_id,
                    SessionGoalState(goal=goal, run_binding=binding),
                )
        return SessionControllerOutput(
            output=f"Goal is now {goal.status}.",
            data={"goal": goal.to_public()},
            owner_state_revision=goal.revision,
        )

    def deactivate_goal_controller(self, call: SessionControllerCall) -> SessionControllerOutput:
        payload = _mapping_payload(call.payload, allow_empty=True)
        with self._lock:
            state = self._state.load(call.session_id)
            goal = state.goal
            if goal is None:
                return SessionControllerOutput(output="No Goal remains.")
            if goal.activation_id != call.activation_id:
                return SessionControllerOutput(
                    output="Superseded Goal work required no cleanup.",
                    data={"goal": goal.to_public(), "superseded": True},
                    owner_state_revision=goal.revision,
                )
            operation = payload.get("operation")
            if operation == "cancel":
                _validate_expected_goal(payload, goal)
                self._state.delete(call.session_id)
                return SessionControllerOutput(
                    output="Goal cancelled.",
                    data={"goal": None, "cancelled": True},
                    owner_state_revision=goal.revision + 1,
                )
            if operation == "complete":
                _validate_expected_goal(payload, goal)
                stamp = utc_now()
                completed = replace(
                    goal,
                    status="complete",
                    report=_required_payload_text(payload, "report"),
                    activation_id=None,
                    revision=goal.revision + 1,
                    updated_at=stamp,
                )
                self._state.save(
                    call.session_id,
                    SessionGoalState(goal=completed, run_binding=state.run_binding),
                )
                return SessionControllerOutput(
                    output="Goal completed.",
                    data={"goal": completed.to_public()},
                    owner_state_revision=completed.revision,
                )
            self._state.delete(call.session_id)
            return SessionControllerOutput(
                output="Goal cancelled because its control was released.",
                data={"goal": None, "cancelled": True},
                owner_state_revision=goal.revision + 1,
            )

    def bind_goal_run(self, call: ContextCall) -> ContextOutput:
        with self._lock:
            state = self._state.load(call.session_id)
            goal = state.goal
            if goal is None or goal.status != "active" or goal.activation_id is None:
                return ContextOutput()
            binding = GoalRunBinding(
                run_id=call.run_id,
                activation_id=goal.activation_id,
                goal_revision=goal.revision,
            )
            if state.run_binding != binding:
                self._state.save(
                    call.session_id,
                    SessionGoalState(goal=goal, run_binding=binding),
                )
        return ContextOutput()

    def goal_input_gate(self, call: PromptPolicyCall) -> PromptPolicyDecision:
        with self._lock:
            goal = self._state.load(call.session_id).goal
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
        if outcome.status not in _TERMINAL_RUN_STATUSES:
            return ContinuationDecision()
        with self._lock:
            state = self._state.load(outcome.session_id)
            goal = state.goal
            binding = state.run_binding
            if goal is None or binding is None or binding.run_id != outcome.run_id:
                return ContinuationDecision()
            usage_tokens = _canonical_usage_tokens(outcome.usage)
            if goal.last_accounted_run_id != outcome.run_id:
                goal = replace(
                    goal,
                    tokens_used=goal.tokens_used + (usage_tokens or 0),
                    last_accounted_run_id=outcome.run_id,
                    revision=goal.revision + 1,
                    updated_at=utc_now(),
                )
                binding = replace(binding, goal_revision=goal.revision)
                self._state.save(
                    outcome.session_id,
                    SessionGoalState(goal=goal, run_binding=binding),
                )
            if goal.status == "complete":
                return ContinuationDecision(
                    action="complete",
                    reason="goal_complete",
                    state_revision=goal.revision,
                )
            if goal.status in {"paused", "blocked", "budget_exhausted"}:
                return ContinuationDecision(
                    action="pause",
                    reason=f"goal_{goal.status}",
                    state_revision=goal.revision,
                )
            if binding.activation_id != goal.activation_id:
                return ContinuationDecision()
            if outcome.status != "completed":
                return self._runtime_stop_decision(
                    goal,
                    binding,
                    operation="runtime_paused",
                    reason=f"run_{outcome.status}",
                    report=outcome.error or f"Work ended with status {outcome.status}.",
                )
            if goal.token_budget is not None and usage_tokens is None:
                return self._runtime_stop_decision(
                    goal,
                    binding,
                    operation="runtime_paused",
                    reason="usage_unavailable",
                    report="Token usage was unavailable, so budgeted work was paused.",
                )
            if goal.token_budget is not None and goal.tokens_used >= goal.token_budget:
                return self._runtime_stop_decision(
                    goal,
                    binding,
                    operation="budget_exhausted",
                    reason="goal_budget_exhausted",
                    report=(
                        f"Token budget exhausted: used {goal.tokens_used} of "
                        f"{goal.token_budget} tokens."
                    ),
                )
            return ContinuationDecision(
                action="continue",
                prompt=continuation_prompt(goal),
                title="Continue Goal",
                role="user",
                scaffold=True,
                semantic_tag="goal-continue",
                coalesce_key="goal-continuation",
                reason="goal_active",
                state_revision=goal.revision,
            )

    @staticmethod
    def _runtime_stop_decision(
        goal: GoalRecord,
        binding: GoalRunBinding,
        *,
        operation: str,
        reason: str,
        report: str,
    ) -> ContinuationDecision:
        assert goal.activation_id is not None
        return ContinuationDecision(
            action="pause",
            reason=reason,
            state_revision=goal.revision,
            session_control_transitions=(
                SessionControlTransition(
                    action="replace",
                    controller_id="mode",
                    replace_activation_ids=(binding.activation_id,),
                    payload={
                        "operation": operation,
                        "expected_activation_id": binding.activation_id,
                        "expected_revision": goal.revision,
                        "report": report,
                    },
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


def _optional_payload_text(value: object) -> str | None:
    if value is None:
        return None
    text = _non_empty_text(value)
    if text is None:
        raise RuntimeError("Goal transition text cannot be blank")
    return text


def _payload_budget(value: object) -> int | None:
    budget, error = _optional_budget(value)
    if error is not None:
        raise RuntimeError(error)
    return budget


def _require_budget_above_usage(token_budget: int | None, tokens_used: int) -> None:
    if token_budget is not None and token_budget <= tokens_used:
        raise RuntimeError("total token budget must be greater than tokens already used")


def _validate_expected_goal(payload: Mapping[str, object], goal: GoalRecord) -> None:
    if payload.get("expected_activation_id") != goal.activation_id:
        raise RuntimeError("Goal transition has a stale activation")
    expected_revision = payload.get("expected_revision")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision != goal.revision
    ):
        raise RuntimeError("Goal transition has a stale revision")


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
