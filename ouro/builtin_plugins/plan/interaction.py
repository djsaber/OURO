"""Wire contract for the builtin plan confirmation interaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from ouro.core.tools import ToolInvocation
from ouro.sdk.plugins.v1 import JsonValue

type PlanConfirmationAction = Literal["accept", "revise", "reject"]

PLAN_CONFIRMATION_TYPE = "plan_confirmation"
PLAN_CONFIRMATION_ACTIONS: tuple[PlanConfirmationAction, ...] = (
    "accept",
    "revise",
    "reject",
)
_PLAN_PLUGIN_ID = "dev.ouro.plan"
_EXIT_PLAN_MODE_TOOL_ID = f"{_PLAN_PLUGIN_ID}/tools/exit"


@dataclass(frozen=True)
class PlanOption:
    label: str
    description: str = ""

    @classmethod
    def from_json(cls, raw: object) -> PlanOption:
        if not isinstance(raw, Mapping):
            raise ValueError("plan option must be an object")
        label = raw.get("label")
        description = raw.get("description", "")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("plan option label must not be empty")
        if not isinstance(description, str):
            raise ValueError("plan option description must be a string")
        return cls(label=label.strip(), description=description.strip())

    def to_json(self) -> dict[str, JsonValue]:
        return {"label": self.label, "description": self.description}


@dataclass(frozen=True)
class PlanConfirmationRequest:
    plan_path: str
    plan: str
    plan_digest: str | None
    options: tuple[PlanOption, ...] = ()
    actions: tuple[PlanConfirmationAction, ...] = PLAN_CONFIRMATION_ACTIONS
    error: str | None = None

    @classmethod
    def from_json(cls, raw: object) -> PlanConfirmationRequest:
        if not isinstance(raw, Mapping) or raw.get("type") != PLAN_CONFIRMATION_TYPE:
            raise ValueError("input request is not a plan confirmation")
        plan_path = raw.get("plan_path")
        plan = raw.get("plan")
        plan_digest = raw.get("plan_digest")
        error = raw.get("error")
        if not isinstance(plan_path, str) or not plan_path:
            raise ValueError("plan confirmation plan_path must not be empty")
        if not isinstance(plan, str):
            raise ValueError("plan confirmation plan must be a string")
        if plan_digest is not None and not isinstance(plan_digest, str):
            raise ValueError("plan confirmation plan_digest must be a string")
        if error is not None and not isinstance(error, str):
            raise ValueError("plan confirmation error must be a string")

        raw_options = raw.get("options", [])
        if not isinstance(raw_options, list):
            raise ValueError("plan confirmation options must be a list")
        options = tuple(PlanOption.from_json(item) for item in raw_options)

        raw_actions = raw.get("actions")
        if not isinstance(raw_actions, list) or not raw_actions:
            raise ValueError("plan confirmation actions must be a non-empty list")
        actions: list[PlanConfirmationAction] = []
        for action in raw_actions:
            if action not in PLAN_CONFIRMATION_ACTIONS:
                raise ValueError(f"unsupported plan confirmation action: {action}")
            actions.append(action)
        if len(set(actions)) != len(actions):
            raise ValueError("plan confirmation actions must not contain duplicates")

        return cls(
            plan_path=plan_path,
            plan=plan,
            plan_digest=plan_digest,
            options=options,
            actions=tuple(actions),
            error=error,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "type": PLAN_CONFIRMATION_TYPE,
            "plan_path": self.plan_path,
            "plan": self.plan,
            "plan_digest": self.plan_digest,
            "options": [option.to_json() for option in self.options],
            "actions": list(self.actions),
            "error": self.error,
        }


@dataclass(frozen=True)
class PlanConfirmationAnswer:
    action: PlanConfirmationAction
    option: str | None = None
    feedback: str | None = None

    @classmethod
    def from_json(cls, raw: object) -> PlanConfirmationAnswer:
        if not isinstance(raw, Mapping):
            raise ValueError("plan confirmation answer must be an object")
        action = raw.get("action")
        option = raw.get("option")
        feedback = raw.get("feedback")
        normalized_action = action.strip().lower() if isinstance(action, str) else ""
        if normalized_action not in PLAN_CONFIRMATION_ACTIONS:
            raise ValueError("plan confirmation action must be accept, revise, or reject")
        if option is not None and not isinstance(option, str):
            raise ValueError("plan confirmation option must be a string")
        if feedback is not None and not isinstance(feedback, str):
            raise ValueError("plan confirmation feedback must be a string")
        return cls(
            action=cast(PlanConfirmationAction, normalized_action),
            option=option.strip() if isinstance(option, str) and option.strip() else None,
            feedback=(feedback.strip() if isinstance(feedback, str) and feedback.strip() else None),
        )

    def to_json(self) -> dict[str, JsonValue]:
        answer: dict[str, JsonValue] = {"action": self.action}
        if self.option is not None:
            answer["option"] = self.option
        if self.feedback is not None:
            answer["feedback"] = self.feedback
        return answer


def parse_builtin_plan_confirmation(
    invocation: ToolInvocation | None,
    data: object,
) -> PlanConfirmationRequest | None:
    """Parse only requests emitted by OURO's canonical builtin ExitPlanMode tool."""

    recognized = (
        invocation is not None
        and invocation.plugin_class == "builtin"
        and invocation.plugin_id == _PLAN_PLUGIN_ID
        and invocation.canonical_tool_id == _EXIT_PLAN_MODE_TOOL_ID
        and isinstance(data, dict)
        and data.get("type") == PLAN_CONFIRMATION_TYPE
    )
    return PlanConfirmationRequest.from_json(data) if recognized else None


__all__ = [
    "PLAN_CONFIRMATION_ACTIONS",
    "PLAN_CONFIRMATION_TYPE",
    "PlanConfirmationAction",
    "PlanConfirmationAnswer",
    "PlanConfirmationRequest",
    "PlanOption",
    "parse_builtin_plan_confirmation",
]
