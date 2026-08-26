from __future__ import annotations

import pytest

from ouro.builtin_plugins.plan.interaction import (
    PlanConfirmationAnswer,
    PlanConfirmationRequest,
    PlanOption,
)
from ouro.cli.plan_confirmation import (
    PlanConfirmationControl,
    collect_plan_confirmation,
    parse_builtin_plan_confirmation,
)
from ouro.core.tools import ToolInvocation


def _request(*options: PlanOption, error: str | None = None) -> PlanConfirmationRequest:
    return PlanConfirmationRequest(
        plan_path="C:/workspace/.ouro/plans/plan.md",
        plan="# Plan\n\nImplement and verify.",
        plan_digest="digest",
        options=options,
        error=error,
    )


def test_collect_plan_confirmation_accepts_selected_option_by_number() -> None:
    request = _request(
        PlanOption(label="安全迁移", description="保留兼容层。"),
        PlanOption(label="直接迁移 🚀", description="一次完成迁移。"),
    )
    inputs = iter(["accept", "2"])
    prompts: list[str] = []
    outputs: list[str] = []

    answer = collect_plan_confirmation(
        request,
        input_func=lambda prompt: prompts.append(prompt) or next(inputs),
        output_func=outputs.append,
    )

    assert answer == {"action": "accept", "option": "直接迁移 🚀"}
    assert prompts == [
        "Plan decision [accept/revise/reject]: ",
        "Select plan option [1-2]: ",
    ]
    assert "2. 直接迁移 🚀 — 一次完成迁移。" in outputs[0]


def test_collect_plan_confirmation_automatically_binds_single_option() -> None:
    request = _request(PlanOption(label="Direct implementation"))

    answer = collect_plan_confirmation(
        request,
        input_func=lambda _prompt: "yes",
        output_func=lambda _text: None,
    )

    assert answer == {"action": "accept", "option": "Direct implementation"}


def test_collect_plan_confirmation_constructs_plain_accept_and_reject_answers() -> None:
    responses = iter(["accept", "reject"])

    accepted = collect_plan_confirmation(
        _request(),
        input_func=lambda _prompt: next(responses),
        output_func=lambda _text: None,
    )
    rejected = collect_plan_confirmation(
        _request(),
        input_func=lambda _prompt: next(responses),
        output_func=lambda _text: None,
    )

    assert accepted == {"action": "accept"}
    assert rejected == {"action": "reject"}


def test_collect_plan_confirmation_collects_revision_feedback_and_renders_error() -> None:
    request = _request(error="The previous answer was incomplete.")
    inputs = iter(["modify", "Add rollback steps."])
    outputs: list[str] = []

    answer = collect_plan_confirmation(
        request,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert answer == {"action": "revise", "feedback": "Add rollback steps."}
    assert "Plan confirmation error: The previous answer was incomplete." in outputs[0]


def test_collect_plan_confirmation_reprompts_invalid_actions_and_options() -> None:
    request = _request(PlanOption(label="A"), PlanOption(label="B"))
    inputs = iter(["later", "accept", "3", "1"])
    outputs: list[str] = []

    answer = collect_plan_confirmation(
        request,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert answer == {"action": "accept", "option": "A"}
    assert "Choose one of: accept, revise, reject." in outputs
    assert "Choose a plan option from 1 to 2." in outputs


def test_collect_plan_confirmation_preserves_abort_and_eof_controls() -> None:
    aborted = collect_plan_confirmation(
        _request(),
        input_func=lambda _prompt: "/abort",
        output_func=lambda _text: None,
    )

    def eof(_prompt: str) -> str:
        raise EOFError

    ended = collect_plan_confirmation(
        _request(),
        input_func=eof,
        output_func=lambda _text: None,
    )

    assert aborted is PlanConfirmationControl.ABORT
    assert ended is PlanConfirmationControl.EOF


def test_plan_confirmation_adapter_requires_official_builtin_provenance() -> None:
    data = _request().to_json()
    official = ToolInvocation(
        session_id="ses_1",
        run_id="run_1",
        tool_name="ExitPlanMode",
        canonical_tool_id="dev.ouro.plan/tools/exit",
        plugin_id="dev.ouro.plan",
        plugin_class="builtin",
    )
    user_plugin = official.model_copy(update={"plugin_class": "user"})

    assert parse_builtin_plan_confirmation(official, data) == _request()
    assert parse_builtin_plan_confirmation(user_plugin, data) is None
    assert parse_builtin_plan_confirmation(official, {"type": "choice"}) is None


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"type": "choice"}, "not a plan confirmation"),
        ({"plan_path": ""}, "plan_path must not be empty"),
        ({"plan": 1}, "plan must be a string"),
        ({"plan_digest": 1}, "plan_digest must be a string"),
        ({"error": 1}, "error must be a string"),
        ({"options": {}}, "options must be a list"),
        ({"options": [None]}, "plan option must be an object"),
        ({"options": [{"label": ""}]}, "option label must not be empty"),
        ({"options": [{"label": "A", "description": 1}]}, "description must be a string"),
        ({"actions": []}, "actions must be a non-empty list"),
        ({"actions": ["defer"]}, "unsupported plan confirmation action"),
        ({"actions": ["accept", "accept"]}, "actions must not contain duplicates"),
    ],
)
def test_plan_confirmation_request_rejects_malformed_wire_data(
    update: dict[str, object],
    message: str,
) -> None:
    data: dict[str, object] = dict(_request().to_json())
    data.update(update)

    with pytest.raises(ValueError, match=message):
        PlanConfirmationRequest.from_json(data)


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (1, "must be an object"),
        ("accept", "must be an object"),
        ({"action": "defer"}, "must be accept, revise, or reject"),
        ({"action": "accept", "option": 1}, "option must be a string"),
        ({"action": "revise", "feedback": 1}, "feedback must be a string"),
    ],
)
def test_plan_confirmation_answer_rejects_malformed_wire_data(
    answer: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PlanConfirmationAnswer.from_json(answer)


def test_plan_confirmation_answer_normalizes_structured_optional_text() -> None:
    revised = PlanConfirmationAnswer.from_json(
        {"action": " revise ", "option": " ", "feedback": " Add tests. "}
    )

    assert revised.to_json() == {"action": "revise", "feedback": "Add tests."}
