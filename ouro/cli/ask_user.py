"""Terminal presentation for the built-in AskUser request."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from pydantic import JsonValue

from ouro.tools.builtin_ask_user import AskUserAnswer, AskUserQuestion, AskUserRequest

InputFunc = Callable[[str], str]
OutputFunc = Callable[[str], None]


class AskUserControl(Enum):
    ABORT = "abort"
    EOF = "eof"


type AskUserInput = dict[str, JsonValue] | AskUserControl


def collect_ask_user_answers(
    request: AskUserRequest,
    *,
    input_func: InputFunc,
    output_func: OutputFunc,
    initial_response: str | None = None,
    first_question_rendered: bool = False,
) -> AskUserInput:
    """Collect and validate one complete answer set for an AskUser request."""

    prefetched = initial_response

    def read(prompt: str) -> str | AskUserControl:
        nonlocal prefetched
        if prefetched is not None:
            value = prefetched
            prefetched = None
            return value
        try:
            return input_func(prompt)
        except EOFError:
            return AskUserControl.EOF

    answers: dict[str, list[str]] = {}
    total = len(request.questions)
    for index, question in enumerate(request.questions):
        if index > 0 or not first_question_rendered:
            output_func(render_ask_user_question(question, index=index, total=total))
        answer = _read_question_answer(
            question,
            read=read,
            output_func=output_func,
        )
        if isinstance(answer, AskUserControl):
            return answer
        answers[question.id] = answer
    return AskUserAnswer(answers=answers).to_json()


def render_ask_user_question(
    question: AskUserQuestion,
    *,
    index: int,
    total: int,
) -> str:
    progress = f"Question {index + 1}/{total}" if total > 1 else "Question"
    lines = [f"{progress} · {question.header}", question.question]
    if question.multi_select:
        lines.append("Select all that apply.")
    lines.append("")
    for option_index, option in enumerate(question.options, start=1):
        lines.append(f"  {option_index}. {option.label}")
        lines.append(f"     {option.description}")
    lines.extend(
        (
            f"  {len(question.options) + 1}. Other",
            "     Type a custom answer.",
        )
    )
    return "\n".join(lines)


def ask_user_answer_prompt(question: AskUserQuestion) -> str:
    last_index = len(question.options) + 1
    if question.multi_select:
        return f"Select one or more [1-{last_index}, comma-separated] or type answers: "
    return f"Select [1-{last_index}] or type an answer: "


def _read_question_answer(
    question: AskUserQuestion,
    *,
    read: Callable[[str], str | AskUserControl],
    output_func: OutputFunc,
) -> list[str] | AskUserControl:
    while True:
        raw = read(ask_user_answer_prompt(question))
        if isinstance(raw, AskUserControl):
            return raw
        if _is_abort(raw):
            return AskUserControl.ABORT
        normalized = raw.strip()
        if not normalized:
            output_func("Answer cannot be empty.")
            continue
        if question.multi_select:
            parsed = _parse_multi_select(question, normalized)
        else:
            parsed = _parse_single_select(question, normalized)
        if isinstance(parsed, str):
            output_func(parsed)
            continue
        answers, needs_other = parsed
        if needs_other:
            custom = _read_custom_answer(read=read, output_func=output_func)
            if isinstance(custom, AskUserControl):
                return custom
            answers.append(custom)
        return _unique_answers(answers)


def _parse_single_select(
    question: AskUserQuestion,
    value: str,
) -> tuple[list[str], bool] | str:
    options = question.options
    if value.isdecimal():
        selected = int(value)
        if 1 <= selected <= len(options):
            return [options[selected - 1].label], False
        if selected == len(options) + 1:
            return [], True
        return f"Choose a number from 1 to {len(options) + 1}, or type a custom answer."

    matching = next(
        (option.label for option in options if option.label.casefold() == value.casefold()),
        None,
    )
    return [matching or value], False


def _parse_multi_select(
    question: AskUserQuestion,
    value: str,
) -> tuple[list[str], bool] | str:
    tokens = [token.strip() for token in value.split(",") if token.strip()]
    if not tokens:
        return "Select at least one option or type a custom answer."

    answers: list[str] = []
    needs_other = False
    other_index = len(question.options) + 1
    labels = {option.label.casefold(): option.label for option in question.options}
    for token in tokens:
        if token.isdecimal():
            selected = int(token)
            if 1 <= selected <= len(question.options):
                answers.append(question.options[selected - 1].label)
                continue
            if selected == other_index:
                needs_other = True
                continue
            return f"Choose numbers from 1 to {other_index}, or type custom answers."
        answers.append(labels.get(token.casefold(), token))
    return answers, needs_other


def _read_custom_answer(
    *,
    read: Callable[[str], str | AskUserControl],
    output_func: OutputFunc,
) -> str | AskUserControl:
    while True:
        raw = read("Custom answer: ")
        if isinstance(raw, AskUserControl):
            return raw
        if _is_abort(raw):
            return AskUserControl.ABORT
        normalized = raw.strip()
        if normalized:
            return normalized
        output_func("Custom answer cannot be empty.")


def _unique_answers(answers: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for answer in answers:
        key = answer.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(answer)
    return unique


def _is_abort(value: str) -> bool:
    return value.strip().lower() in {"/abort", "abort"}


__all__ = [
    "AskUserControl",
    "AskUserInput",
    "ask_user_answer_prompt",
    "collect_ask_user_answers",
    "render_ask_user_question",
]
