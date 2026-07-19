"""Terminal presentation for the builtin plan mode confirmation request."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from ouro.builtin_plugins.plan_mode.interaction import (
    PLAN_CONFIRMATION_TYPE,
    PlanConfirmationAction,
    PlanConfirmationAnswer,
    PlanConfirmationRequest,
    PlanOption,
)
from ouro.cli.interactive_output import OutputFunc
from ouro.core.tools import ToolInvocation
from ouro.sdk.plugins.v1 import JsonValue

InputFunc = Callable[[str], str]

_PLAN_MODE_PLUGIN_ID = "dev.ouro.plan_mode"
_EXIT_PLAN_MODE_TOOL_ID = f"{_PLAN_MODE_PLUGIN_ID}/tools/exit"


class PlanConfirmationControl(Enum):
    ABORT = "abort"
    EOF = "eof"


type PlanConfirmationInput = dict[str, JsonValue] | PlanConfirmationControl


def parse_builtin_plan_confirmation(
    invocation: ToolInvocation | None,
    data: object,
) -> PlanConfirmationRequest | None:
    """Parse requests only when they come from OURO's builtin ExitPlanMode tool."""

    recognized = (
        invocation is not None
        and invocation.plugin_class == "builtin"
        and invocation.plugin_id == _PLAN_MODE_PLUGIN_ID
        and invocation.canonical_tool_id == _EXIT_PLAN_MODE_TOOL_ID
        and isinstance(data, dict)
        and data.get("type") == PLAN_CONFIRMATION_TYPE
    )
    return PlanConfirmationRequest.from_json(data) if recognized else None


def collect_plan_confirmation(
    request: PlanConfirmationRequest,
    *,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> PlanConfirmationInput:
    """Render one plan request and construct the plugin's structured answer."""

    output_func(_render_plan_confirmation(request))
    action = _read_action(request, input_func=input_func, output_func=output_func)
    if isinstance(action, PlanConfirmationControl):
        return action

    option: str | None = None
    feedback: str | None = None
    if action == "accept" and request.options:
        selected = _read_option(request.options, input_func=input_func, output_func=output_func)
        if isinstance(selected, PlanConfirmationControl):
            return selected
        option = selected
    elif action == "revise":
        collected = _read_line(input_func, "Revision feedback (optional): ")
        if isinstance(collected, PlanConfirmationControl):
            return collected
        if _is_abort(collected):
            return PlanConfirmationControl.ABORT
        feedback = collected.strip() or None

    return PlanConfirmationAnswer(
        action=action,
        option=option,
        feedback=feedback,
    ).to_json()


def _render_plan_confirmation(request: PlanConfirmationRequest) -> str:
    lines = [
        "Plan approval requested.",
        f"Plan file: {request.plan_path}",
        "",
        request.plan,
    ]
    if request.options:
        lines.extend(("", "Plan options:"))
        for index, option in enumerate(request.options, start=1):
            suffix = f" — {option.description}" if option.description else ""
            lines.append(f"  {index}. {option.label}{suffix}")
    if request.error:
        lines.extend(("", f"Plan confirmation error: {request.error}"))
    return "\n".join(lines)


def _read_action(
    request: PlanConfirmationRequest,
    *,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> PlanConfirmationAction | PlanConfirmationControl:
    aliases: dict[str, PlanConfirmationAction] = {
        "a": "accept",
        "accept": "accept",
        "y": "accept",
        "yes": "accept",
        "m": "revise",
        "modify": "revise",
        "v": "revise",
        "revise": "revise",
        "n": "reject",
        "no": "reject",
        "r": "reject",
        "reject": "reject",
    }
    available = set(request.actions)
    choices = "/".join(request.actions)
    while True:
        raw = _read_line(input_func, f"Plan decision [{choices}]: ")
        if isinstance(raw, PlanConfirmationControl):
            return raw
        if _is_abort(raw):
            return PlanConfirmationControl.ABORT
        action = aliases.get(raw.strip().lower())
        if action in available:
            return action
        output_func(f"Choose one of: {', '.join(request.actions)}.")


def _read_option(
    options: tuple[PlanOption, ...],
    *,
    input_func: InputFunc,
    output_func: OutputFunc,
) -> str | PlanConfirmationControl:
    if len(options) == 1:
        return options[0].label
    labels = {option.label: option.label for option in options}
    while True:
        raw = _read_line(input_func, f"Select plan option [1-{len(options)}]: ")
        if isinstance(raw, PlanConfirmationControl):
            return raw
        if _is_abort(raw):
            return PlanConfirmationControl.ABORT
        normalized = raw.strip()
        if normalized.isdigit():
            index = int(normalized)
            if 1 <= index <= len(options):
                return options[index - 1].label
        if normalized in labels:
            return labels[normalized]
        output_func(f"Choose a plan option from 1 to {len(options)}.")


def _read_line(input_func: InputFunc, prompt: str) -> str | PlanConfirmationControl:
    try:
        return input_func(prompt)
    except EOFError:
        return PlanConfirmationControl.EOF


def _is_abort(value: str) -> bool:
    return value.strip().lower() in {"/abort", "abort"}


__all__ = [
    "PlanConfirmationControl",
    "PlanConfirmationInput",
    "collect_plan_confirmation",
    "parse_builtin_plan_confirmation",
]
