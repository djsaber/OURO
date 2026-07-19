from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ouro.sdk.plugins.v1 import (
    CommandContribution,
    CommandOutput,
    ContextContribution,
    ContextFragment,
    ContextOutput,
    ContributionSet,
    PluginContext,
    ToolContribution,
    ToolInputRequest,
    ToolOutput,
    ToolPolicyContribution,
    ToolPolicyDecision,
    ToolResumeCall,
)

from .interaction import (
    PLAN_CONFIRMATION_ACTIONS,
    PlanConfirmationAnswer,
    PlanConfirmationRequest,
    PlanOption,
)
from .state import (
    PlanStateRepository,
    SessionPlanState,
    validate_session_id,
)

_ACTIVE_PHASES = {"active", "awaiting_approval"}
_OWN_TOOLS = {
    "dev.ouro.plan_mode/tools/enter",
    "dev.ouro.plan_mode/tools/exit",
}
_PLAN_WRITERS = {
    "ouro.host/tools/Write",
    "ouro.host/tools/Edit",
}

_SCAFFOLD = """Plan mode is active. You MUST NOT make any edits (with the exception of the
current plan file) or otherwise make changes to the environment, external
systems, or any state, unless a tool request is explicitly approved. Prefer
read-only tools. Tools that could modify state or produce side effects follow
the normal permission mode and rules. This supersedes any other instructions
you have received. Any other tools that would break plan mode — state-mutating,
side-effecting, or irreversible actions — are also blocked; call ExitPlanMode
first if you need them.

Workflow:

1. Understand — explore the relevant context using read-only tools.
2. Design — converge on one concrete recommendation and its trade-offs.
3. Verify — re-check the key facts and affected files.
4. Write Plan — record the plan in the designated plan file; this is the only
   file you may modify without separate approval.
5. Exit — call ExitPlanMode for user approval.

When the plan contains genuinely different implementation approaches, provide
at most three approaches as options to ExitPlanMode. Options are implementation
approaches, not approval actions. Never create options for approving, revising,
or rejecting the plan; the Host provides those actions separately. Use AskUser
only for missing requirements or preferences. Never ask for plan approval
through text or AskUser, and do not refer to \"the plan\" in an AskUser question.

Plan file: {plan_path}"""

_ENTERED = """Plan mode is now active. Your workflow:

Plan file: {plan_path}

1. Use read-only tools to investigate the codebase.
2. Design a concrete, step-by-step plan.
3. Write the plan to the plan file with the shared Write or Edit tool.
4. When the plan is ready, call ExitPlanMode for user approval.

Do not edit other files while plan mode is active unless that exact tool call
is separately approved."""


class PlanModePlugin:
    def __init__(self, context: PluginContext) -> None:
        self._state = PlanStateRepository(context.data)
        self._lock = threading.RLock()

    async def prepare(self) -> ContributionSet:
        return ContributionSet(
            tools=[
                ToolContribution(
                    id="enter",
                    exposed_name="EnterPlanMode",
                    description=(
                        "Use this tool proactively before starting a non-trivial "
                        "implementation task."
                    ),
                    handler=self.enter_plan_mode,
                    parameters_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    permission_mode="auto",
                    permission_control="tool",
                    effect="state_changing",
                    batch_behavior="exclusive",
                ),
                ToolContribution(
                    id="exit",
                    exposed_name="ExitPlanMode",
                    description="Submit the current plan file for user approval.",
                    handler=self.exit_plan_mode,
                    resume_handler=self.resume_exit_plan_mode,
                    parameters_schema={
                        "type": "object",
                        "properties": {
                            "options": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3,
                                "description": (
                                    "Optional mutually exclusive implementation approaches "
                                    "from the plan. Never create options for approving, "
                                    "revising, or rejecting the plan; the Host provides those "
                                    "actions separately."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 80,
                                        },
                                        "description": {
                                            "type": "string",
                                            "default": "",
                                        },
                                    },
                                    "required": ["label"],
                                    "additionalProperties": False,
                                },
                            }
                        },
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
                    id="plan",
                    description="Enter plan mode for the current Session.",
                    handler=self.plan_command,
                )
            ],
            context_providers=[
                ContextContribution(
                    id="plan-scaffold",
                    handler=self.plan_context,
                    priority=10,
                    timeout_seconds=5,
                    failure_mode="block",
                )
            ],
            tool_policies=[
                ToolPolicyContribution(
                    id="plan-tool-gate",
                    handler=self.plan_tool_gate,
                    priority=10,
                    timeout_seconds=5,
                )
            ],
        )

    async def start(self) -> None:
        return None

    async def stop(self, reason, deadline) -> None:
        del reason, deadline

    def enter_plan_mode(self, call) -> ToolOutput:
        with self._lock:
            current = self._state.load(call.session_id)
            if current.phase in _ACTIVE_PHASES:
                if (
                    current.activation_tool_call_id == call.tool_call_id
                    and current.plan_path is not None
                ):
                    return ToolOutput(
                        content=_ENTERED.format(plan_path=current.plan_path),
                        structured={"phase": current.phase, "plan_path": current.plan_path},
                    )
                return ToolOutput(
                    content="Plan mode is already active. Use ExitPlanMode when ready.",
                    structured={"phase": current.phase, "plan_path": current.plan_path},
                    is_error=True,
                )
            state = self._new_active_state(
                call.session_id,
                call.workspace,
                previous=current,
                activation_tool_call_id=call.tool_call_id,
            )
            self._state.save(call.session_id, state)
        assert state.plan_path is not None
        return ToolOutput(
            content=_ENTERED.format(plan_path=state.plan_path),
            structured={"phase": state.phase, "plan_path": state.plan_path},
        )

    def plan_command(self, call) -> CommandOutput:
        if call.session_id is None:
            return CommandOutput(output="The /plan command requires a target Session.")
        with self._lock:
            current = self._state.load(call.session_id)
            if current.phase in _ACTIVE_PHASES:
                state = current
                activated = False
            else:
                state = self._new_active_state(
                    call.session_id,
                    call.workspace,
                    previous=current,
                    activation_tool_call_id=None,
                )
                activated = True
            self._state.save(call.session_id, state)
        assert state.plan_path is not None
        argument_notice = (
            " The /plan command only changes mode; send the planning request as a separate message."
            if call.argument.strip()
            else ""
        )
        return CommandOutput(
            output=(
                f"Plan mode activated. Plan file: {state.plan_path}"
                if activated
                else f"Plan mode is already active. Plan file: {state.plan_path}"
            )
            + argument_notice,
            data={"phase": state.phase, "plan_path": state.plan_path},
        )

    def plan_context(self, call) -> ContextOutput:
        with self._lock:
            state = self._state.load(call.session_id)
            if state.phase not in _ACTIVE_PHASES or state.plan_path is None:
                return ContextOutput()
            scaffold = _SCAFFOLD.format(plan_path=state.plan_path)
        return ContextOutput(fragments=(ContextFragment(text=scaffold, persistence="message"),))

    def plan_tool_gate(self, call) -> ToolPolicyDecision:
        with self._lock:
            state = self._state.load(call.session_id)
        if state.phase not in _ACTIVE_PHASES:
            return ToolPolicyDecision()
        if call.canonical_tool_id in _OWN_TOOLS or call.effect == "read_only":
            return ToolPolicyDecision()
        if (
            call.canonical_tool_id in _PLAN_WRITERS
            and state.plan_path is not None
            and self._targets_plan_file(call.arguments, call.workspace, state.plan_path)
        ):
            return ToolPolicyDecision()
        return ToolPolicyDecision(
            action="require_approval",
            reason=(
                "Plan mode is active. This state-changing or unknown-effect tool call "
                "requires explicit approval because it does not exclusively target the "
                "current plan file."
            ),
        )

    def exit_plan_mode(self, call) -> ToolOutput | ToolInputRequest:
        with self._lock:
            state = self._state.load(call.session_id)
            if (
                state.phase == "awaiting_approval"
                and state.approval_tool_call_id == call.tool_call_id
                and state.plan_path is not None
            ):
                plan, error = _read_plan(state.plan_path)
                if error is not None:
                    self._state.save(call.session_id, _clear_pending_approval(state))
                    return ToolOutput(content=error, is_error=True)
                if _plan_digest(plan) != state.approval_digest:
                    self._state.save(call.session_id, _clear_pending_approval(state))
                    return ToolOutput(
                        content=(
                            "The plan file changed while ExitPlanMode was being recovered. "
                            "Call ExitPlanMode again."
                        ),
                        is_error=True,
                    )
                return _approval_request(state, plan=plan)
            if state.phase != "active" or state.plan_path is None:
                return ToolOutput(
                    content="Plan mode is not active. Call EnterPlanMode first.",
                    is_error=True,
                )
            plan, error = _read_plan(state.plan_path)
            if error is not None:
                return ToolOutput(content=error, is_error=True)
            options = _normalize_options(call.arguments.get("options"))
            digest = _plan_digest(plan)
            awaiting = replace(
                state,
                phase="awaiting_approval",
                revision=state.revision + 1,
                approval_tool_call_id=call.tool_call_id,
                approval_digest=digest,
                options=options,
                last_resume_tool_call_id=None,
                last_resume_output=None,
            )
            self._state.save(call.session_id, awaiting)
        return _approval_request(awaiting, plan=plan)

    def resume_exit_plan_mode(self, call: ToolResumeCall) -> ToolOutput | ToolInputRequest:
        with self._lock:
            state = self._state.load(call.session_id)
            if (
                state.last_resume_tool_call_id == call.tool_call_id
                and state.last_resume_output is not None
            ):
                return _tool_output_from_json(state.last_resume_output)
            if (
                state.phase != "awaiting_approval"
                or state.approval_tool_call_id != call.tool_call_id
                or state.plan_path is None
            ):
                return ToolOutput(
                    content="This plan approval request is no longer active.",
                    is_error=True,
                )
            plan, error = _read_plan(state.plan_path)
            if error is not None:
                output = ToolOutput(content=error, is_error=True)
                self._finish_resume(call.session_id, state, output, phase="active")
                return output
            if _plan_digest(plan) != state.approval_digest:
                output = ToolOutput(
                    content=(
                        "The plan file changed after the approval prompt was created. "
                        "Review it and call ExitPlanMode again."
                    ),
                    is_error=True,
                )
                self._finish_resume(call.session_id, state, output, phase="active")
                return output
            try:
                answer = PlanConfirmationAnswer.from_json(call.answer)
            except ValueError:
                return _approval_request(
                    state,
                    plan=plan,
                    error="Choose accept, revise, or reject.",
                )
            action = answer.action
            selected_option = answer.option
            feedback = answer.feedback
            if action == "accept" and state.options:
                labels = {option.label for option in state.options}
                if selected_option not in labels:
                    return _approval_request(
                        state,
                        plan=plan,
                        error="Select one of the proposed plan options before accepting.",
                    )
            if action == "accept":
                selected = (
                    f"\nSelected option: {selected_option}\n" if selected_option is not None else ""
                )
                output = ToolOutput(
                    content=(
                        "Exited plan mode. Plan mode deactivated. All tools are now "
                        f"available.\nPlan saved to: {state.plan_path}\n{selected}\n{plan}"
                    ),
                    structured={
                        "phase": "inactive",
                        "plan_path": state.plan_path,
                        "plan": plan,
                        "selected_option": selected_option,
                    },
                )
                self._finish_resume(call.session_id, state, output, phase="inactive")
                return output
            if action == "revise":
                suffix = f" Feedback: {feedback}" if feedback else ""
                output = ToolOutput(
                    content=f"Plan revision requested by user.{suffix}",
                    structured={"phase": "active", "action": "revise", "feedback": feedback},
                    is_error=True,
                )
            else:
                output = ToolOutput(
                    content="Plan rejected by user. Plan mode remains active.",
                    structured={"phase": "active", "action": "reject"},
                    is_error=True,
                )
            self._finish_resume(call.session_id, state, output, phase="active")
            return output

    def _finish_resume(
        self,
        session_id: str,
        state: SessionPlanState,
        output: ToolOutput,
        *,
        phase: Literal["inactive", "active"],
    ) -> None:
        completed = replace(
            state,
            phase=phase,
            revision=state.revision + 1,
            approval_tool_call_id=None,
            approval_digest=None,
            options=(),
            last_resume_tool_call_id=state.approval_tool_call_id,
            last_resume_output=_tool_output_to_json(output),
        )
        self._state.save(session_id, completed)

    @staticmethod
    def _new_active_state(
        session_id: str,
        workspace: str | None,
        *,
        previous: SessionPlanState,
        activation_tool_call_id: str | None,
    ) -> SessionPlanState:
        safe_session_id = validate_session_id(session_id)
        root = Path(workspace or Path.cwd()).expanduser().resolve(strict=False)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        plan_path = (
            root
            / ".ouro"
            / "sessions"
            / safe_session_id
            / "plans"
            / f"plan_{stamp}_{uuid4().hex[:8]}.md"
        ).resolve(strict=False)
        return SessionPlanState(
            phase="active",
            plan_path=str(plan_path),
            revision=previous.revision + 1,
            activation_tool_call_id=activation_tool_call_id,
        )

    @staticmethod
    def _targets_plan_file(
        arguments: Mapping[str, object],
        workspace: str | None,
        plan_path: str,
    ) -> bool:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return False
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(workspace or Path.cwd()) / candidate
        return candidate.resolve(strict=False) == Path(plan_path).resolve(strict=False)


def _approval_request(
    state: SessionPlanState,
    *,
    plan: str,
    error: str | None = None,
) -> ToolInputRequest:
    if state.plan_path is None:
        raise ValueError("plan confirmation requires an active plan path")
    return ToolInputRequest(
        data=PlanConfirmationRequest(
            plan_path=state.plan_path,
            plan=plan,
            plan_digest=state.approval_digest,
            options=state.options,
            actions=PLAN_CONFIRMATION_ACTIONS,
            error=error,
        ).to_json()
    )


def _clear_pending_approval(state: SessionPlanState) -> SessionPlanState:
    return replace(
        state,
        phase="active",
        revision=state.revision + 1,
        approval_tool_call_id=None,
        approval_digest=None,
        options=(),
    )


def _normalize_options(raw: object) -> tuple[PlanOption, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("ExitPlanMode options must be a list")
    options: list[PlanOption] = []
    for item in raw:
        try:
            options.append(PlanOption.from_json(item))
        except ValueError as exc:
            raise ValueError(f"ExitPlanMode {exc}") from exc
    return tuple(options)


def _read_plan(plan_path: str) -> tuple[str, str | None]:
    path = Path(plan_path)
    try:
        plan = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "", f"No plan file found. Write your plan to {path} first."
    except OSError:
        return "", f"The plan file could not be read: {path}"
    if not plan:
        return "", f"Plan file is empty. Write a non-empty plan to {path} first."
    return plan, None


def _plan_digest(plan: str) -> str:
    return hashlib.sha256(plan.encode("utf-8")).hexdigest()


def _tool_output_to_json(output: ToolOutput) -> dict[str, object]:
    return {
        "content": output.content,
        "structured": dict(output.structured),
        "resource_refs": list(output.resource_refs),
        "is_error": output.is_error,
    }


def _tool_output_from_json(raw: Mapping[str, object]) -> ToolOutput:
    structured = raw.get("structured", {})
    resource_refs = raw.get("resource_refs", [])
    return ToolOutput(
        content=str(raw.get("content", "")),
        structured=dict(structured) if isinstance(structured, Mapping) else {},
        resource_refs=(
            tuple(str(item) for item in resource_refs) if isinstance(resource_refs, list) else ()
        ),
        is_error=bool(raw.get("is_error", False)),
    )


def create_plugin(context: PluginContext) -> PlanModePlugin:
    return PlanModePlugin(context)
