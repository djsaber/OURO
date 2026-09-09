from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ouro.sdk.plugins.v2 import (
    CommandContribution,
    CommandOutput,
    ContextContribution,
    ContextFragment,
    ContextOutput,
    ContributionSet,
    PluginContext,
    SessionControlClaim,
    SessionControllerContribution,
    SessionControllerOutput,
    SessionControlTransition,
    ToolContribution,
    ToolInputRequest,
    ToolOutput,
    ToolPolicyContribution,
    ToolPolicyDecision,
    ToolResumeCall,
    UnattendedResolution,
)

from .interaction import (
    PlanConfirmationAnswer,
    PlanConfirmationRequest,
    PlanOption,
)
from .state import (
    PlanStateRepository,
    SessionPlanState,
    validate_session_id,
)

_OWN_TOOLS = {
    "dev.ouro.plan/tools/enter",
    "dev.ouro.plan/tools/exit",
}
_PLAN_WRITERS = {
    "ouro.host/tools/Write",
    "ouro.host/tools/Edit",
}

ENTER_PLAN_MODE_DESCRIPTION = (
    "Use this tool before a non-trivial implementation when you need read-only investigation "
    "and an explicit plan before making changes. Call it for ambiguous scope, architectural "
    "decisions, or multi-file work whose approach should be reviewed. Do not call it for small, "
    "well-specified edits, purely informational requests, or when plan mode is already active."
)

EXIT_PLAN_MODE_DESCRIPTION = (
    "Use this tool only after the plan file contains a complete, actionable implementation plan "
    "and you are ready for user review. Do not call it before writing the plan or use it to ask "
    "for missing requirements; use AskUser for those. This submits the plan for acceptance, "
    "revision, or rejection. Optional options represent implementation alternatives, never "
    "approval actions."
)

_SCAFFOLD = """Plan mode is active. Read-only tools are available without additional plan
mode restrictions. To create or change the plan, you MUST use OURO's builtin
Write or Edit tool and target the exact plan file shown below. Those Write/Edit
calls are denied for every other path. Every other state-changing or
unknown-effect tool is denied regardless of target. Do not use shell, MCP,
plugin, or other tools to make changes, even if they could write the plan file.
This supersedes any other instructions you have received. Call ExitPlanMode
before performing implementation work or making any other change.

Workflow:

1. Understand — explore the relevant context using read-only tools.
2. Design — converge on one concrete recommendation and its trade-offs.
3. Verify — re-check the key facts and affected files.
4. Write Plan — use only OURO's builtin Write or Edit tool to record the plan in
   the designated plan file; no other file may be modified.
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

1. Use any read-only tools to investigate the codebase.
2. Design a concrete, step-by-step plan.
3. Write the plan with OURO's builtin Write or Edit tool, targeting exactly the
   plan file above.
4. When the plan is ready, call ExitPlanMode for user approval.

All other state-changing or unknown-effect tool calls are denied while plan
mode is active. Approval cannot override this restriction."""


class PlanPlugin:
    def __init__(self, context: PluginContext) -> None:
        if context.artifact_root is None:
            raise RuntimeError("plan mode requires a Host-managed artifact root")
        self._artifact_root = Path(context.artifact_root).expanduser().resolve(strict=False)

    async def prepare(self) -> ContributionSet:
        return ContributionSet(
            tools=[
                ToolContribution(
                    id="enter",
                    exposed_name="EnterPlanMode",
                    description=ENTER_PLAN_MODE_DESCRIPTION,
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
                    description=EXIT_PLAN_MODE_DESCRIPTION,
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
                                    "One to three mutually exclusive implementation approaches "
                                    "already explained in the plan. Omit when the plan recommends "
                                    "a single approach. Never use options for approving, revising, "
                                    "or rejecting the plan."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 80,
                                            "description": (
                                                "Concise user-facing name for this implementation "
                                                "approach."
                                            ),
                                        },
                                        "description": {
                                            "type": "string",
                                            "default": "",
                                            "description": (
                                                "Short sentence explaining the approach's defining "
                                                "trade-off."
                                            ),
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
                    supports_unattended=True,
                    interaction_controller="mode",
                ),
            ],
            commands=[
                CommandContribution(
                    id="plan",
                    description="Enter plan mode for the current Session.",
                    usage="[on|off|status]",
                    handler=self.plan_command,
                )
            ],
            session_controllers=[
                SessionControllerContribution(
                    id="mode",
                    description="Own the Plan mode lifecycle for one Session.",
                    claims=(
                        SessionControlClaim(
                            domain="ouro.session.primary_behavior",
                            access="exclusive",
                        ),
                    ),
                    activate=self.activate_plan_controller,
                    deactivate=self.deactivate_plan_controller,
                )
            ],
            context_providers=[
                ContextContribution(
                    id="plan-scaffold",
                    handler=self.plan_context,
                    priority=10,
                    timeout_seconds=5,
                    failure_mode="block",
                    active_when="mode",
                )
            ],
            tool_policies=[
                ToolPolicyContribution(
                    id="plan-tool-gate",
                    handler=self.plan_tool_gate,
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

    def enter_plan_mode(self, call) -> ToolOutput:
        return ToolOutput(
            session_control_transitions=(
                SessionControlTransition(
                    action="acquire",
                    controller_id="mode",
                ),
            ),
        )

    def plan_command(self, call) -> CommandOutput:
        repository = PlanStateRepository(call.session_state)
        if call.session_id is None:
            return CommandOutput(output="The /plan command requires a target Session.")
        argument = call.argument.strip().lower()
        if argument == "off":
            return CommandOutput(
                session_control_transitions=(
                    SessionControlTransition(
                        action="release",
                        controller_id="mode",
                        reason="user_command",
                    ),
                ),
            )
        if argument == "status":
            state = repository.load()
            phase = "active" if repository.active else "inactive"
            return CommandOutput(
                output=(
                    f"Plan mode phase: {phase}."
                    + (f" Plan file: {state.plan_path}" if state.plan_path else "")
                ),
                data={"phase": phase, "plan_path": state.plan_path},
            )
        argument_notice = (
            " The /plan command only changes mode; send the planning request as a separate message."
            if argument and argument != "on"
            else ""
        )
        return CommandOutput(
            output=argument_notice.strip(),
            session_control_transitions=(
                SessionControlTransition(
                    action="acquire",
                    controller_id="mode",
                ),
            ),
        )

    def activate_plan_controller(self, call) -> SessionControllerOutput:
        repository = PlanStateRepository(call.session_state)
        state = self._new_active_state(call.session_id)
        repository.save(state)
        assert state.plan_path is not None
        return SessionControllerOutput(
            output=f"Plan mode activated. Plan file: {state.plan_path}",
            data={"phase": "active", "plan_path": state.plan_path},
        )

    def deactivate_plan_controller(self, call) -> SessionControllerOutput:
        repository = PlanStateRepository(call.session_state)
        state = repository.load()
        return SessionControllerOutput(
            output="Plan mode deactivated. All tools are now available for the next round.",
            data={
                "phase": "inactive",
                "plan_path": state.plan_path,
            },
        )

    def plan_context(self, call) -> ContextOutput:
        repository = PlanStateRepository(call.session_state)
        state = repository.load()
        if state.plan_path is None:
            return ContextOutput()
        scaffold = _SCAFFOLD.format(plan_path=state.plan_path)
        return ContextOutput(
            fragments=(
                ContextFragment(
                    text=scaffold,
                    persistence="message",
                    role="system",
                    scaffold=True,
                    semantic_tag="plan-context",
                ),
            )
        )

    def plan_tool_gate(self, call) -> ToolPolicyDecision:
        repository = PlanStateRepository(call.session_state)
        state = repository.load()
        if call.canonical_tool_id in _OWN_TOOLS:
            return ToolPolicyDecision()
        if call.canonical_tool_id in _PLAN_WRITERS:
            if state.plan_path is not None and self._targets_plan_file(
                call.arguments,
                call.workspace,
                state.plan_path,
            ):
                return ToolPolicyDecision()
            return ToolPolicyDecision(
                action="deny",
                reason=(
                    "Plan mode denied this Write or Edit call because it does not "
                    "exclusively target the current plan file."
                ),
            )
        if call.effect == "read_only":
            return ToolPolicyDecision()
        return ToolPolicyDecision(
            action="deny",
            reason=(
                "Plan mode denied this state-changing or unknown-effect tool call. "
                "Only OURO's builtin Write or Edit may modify the current plan file; "
                "read-only tools remain available."
            ),
        )

    def exit_plan_mode(self, call) -> ToolOutput | ToolInputRequest:
        repository = PlanStateRepository(call.session_state)
        state = repository.load()
        if not repository.active or state.plan_path is None:
            return ToolOutput(
                content="Plan mode is not active. Call EnterPlanMode first.",
                is_error=True,
            )
        plan, error = _read_plan(state.plan_path)
        if error is not None:
            return ToolOutput(content=error, is_error=True)
        return ToolInputRequest(
            data=PlanConfirmationRequest(
                plan_path=state.plan_path,
                plan=plan,
                plan_digest=_plan_digest(plan),
                options=_normalize_options(call.arguments.get("options")),
            ).to_json()
        )

    def resume_exit_plan_mode(self, call: ToolResumeCall) -> ToolOutput | ToolInputRequest:
        repository = PlanStateRepository(call.session_state)
        state = repository.load()
        request = PlanConfirmationRequest.from_json(call.request)
        if not repository.active or state.plan_path != request.plan_path:
            return ToolOutput(
                content="This plan approval request is no longer active.",
                is_error=True,
            )
        plan, error = _read_plan(request.plan_path)
        if error is not None:
            return ToolOutput(content=error, is_error=True)
        if _plan_digest(plan) != request.plan_digest:
            return ToolOutput(
                content=(
                    "The plan file changed after the approval prompt was created. "
                    "Review it and call ExitPlanMode again."
                ),
                is_error=True,
            )
        if isinstance(call.resolution, UnattendedResolution):
            selected_option = request.options[0].label if len(request.options) == 1 else None
            option_selection = (
                "only_option"
                if selected_option is not None
                else "agent_discretion"
                if request.options
                else "not_applicable"
            )
            selected = (
                f"\nSelected option by AFK policy: {selected_option}\n"
                if selected_option is not None
                else ""
            )
            output = ToolOutput(
                content=(
                    "Exited plan mode under AFK policy. User approval was not "
                    "requested; execution is delegated to the agent.\n"
                    f"Plan saved to: {state.plan_path}\n{selected}\n{plan}"
                ),
                structured={
                    "phase": "inactive",
                    "plan_path": state.plan_path,
                    "plan": plan,
                    "decision": "proceed",
                    "resolved_by": call.resolution.resolved_by,
                    "resolver_id": call.resolution.resolver_id,
                    "selected_option": selected_option,
                    "option_selection": option_selection,
                },
            )
            output = replace(
                output,
                session_control_transitions=(
                    SessionControlTransition(
                        action="release",
                        controller_id="mode",
                        reason="unattended_plan_exit",
                    ),
                ),
            )
            return output
        try:
            answer = PlanConfirmationAnswer.from_json(call.answer)
        except ValueError:
            return _approval_request(
                request,
                error="Choose accept, revise, or reject.",
            )
        action = answer.action
        selected_option = answer.option
        feedback = answer.feedback
        if action == "accept" and request.options:
            labels = {option.label for option in request.options}
            if selected_option not in labels:
                return _approval_request(
                    request,
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
            output = replace(
                output,
                session_control_transitions=(
                    SessionControlTransition(
                        action="release",
                        controller_id="mode",
                        reason="plan_accepted",
                    ),
                ),
            )
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
        return output

    def _new_active_state(self, session_id: str) -> SessionPlanState:
        safe_session_id = validate_session_id(session_id)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        plan_path = (
            self._artifact_root
            / "sessions"
            / safe_session_id
            / "plans"
            / f"plan_{stamp}_{uuid4().hex[:8]}.md"
        ).resolve(strict=False)
        return SessionPlanState(plan_path=str(plan_path))

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


def _approval_request(request: PlanConfirmationRequest, *, error: str) -> ToolInputRequest:
    return ToolInputRequest(data=replace(request, error=error).to_json())


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


def create_plugin(context: PluginContext) -> PlanPlugin:
    return PlanPlugin(context)
