from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import JsonValue

from ouro.tools.builtin_ask_user import parse_builtin_ask_user_request

AnswerMode = Literal["mapping", "value"]


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class QuestionField:
    question_id: str
    header: str
    prompt: str
    options: tuple[QuestionOption, ...] = ()
    multi_select: bool = False


@dataclass(frozen=True)
class QuestionForm:
    questions: tuple[QuestionField, ...]
    answer_mode: AnswerMode

    def encode(self, answers: dict[str, list[str]]) -> JsonValue:
        if self.answer_mode == "mapping":
            return cast(JsonValue, {"answers": answers})
        question = self.questions[0]
        values = answers[question.question_id]
        return cast(JsonValue, values if question.multi_select else values[0])


def project_question_form(tool_name: str, request: object) -> QuestionForm:
    """Project built-in and common plugin input requests into one Host form."""

    try:
        builtin = parse_builtin_ask_user_request(tool_name, request)
    except ValueError:
        builtin = None
    if builtin is not None:
        return QuestionForm(
            questions=tuple(
                QuestionField(
                    question_id=question.id,
                    header=question.header,
                    prompt=question.question,
                    options=tuple(
                        QuestionOption(option.label, option.description)
                        for option in question.options
                    ),
                    multi_select=question.multi_select,
                )
                for question in builtin.questions
            ),
            answer_mode="mapping",
        )

    questions = _question_list(request)
    if questions:
        return QuestionForm(questions=questions, answer_mode="mapping")

    single = _single_question(tool_name, request)
    return QuestionForm(questions=(single,), answer_mode="value")


def _question_list(request: object) -> tuple[QuestionField, ...]:
    if not isinstance(request, dict):
        return ()
    raw_questions = request.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 3:
        return ()
    questions: list[QuestionField] = []
    for index, raw_question in enumerate(raw_questions):
        question = _question_from_mapping(raw_question, fallback_id=f"question_{index + 1}")
        if question is None:
            return ()
        questions.append(question)
    return tuple(questions)


def _single_question(tool_name: str, request: object) -> QuestionField:
    if isinstance(request, dict):
        projected = _question_from_mapping(request, fallback_id="answer")
        if projected is not None:
            return projected
    prompt = (
        request.strip()
        if isinstance(request, str) and request.strip()
        else _json_preview(request)
    )
    return QuestionField(
        question_id="answer",
        header=_bounded_header(tool_name),
        prompt=prompt or "Provide the information required to continue.",
    )


def _question_from_mapping(request: object, *, fallback_id: str) -> QuestionField | None:
    if not isinstance(request, dict):
        return None
    prompt = next(
        (
            value.strip()
            for key in ("question", "prompt", "message", "title")
            if isinstance((value := request.get(key)), str) and value.strip()
        ),
        None,
    )
    if prompt is None:
        return None
    raw_id = request.get("id")
    question_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else fallback_id
    raw_header = request.get("header")
    header = (
        raw_header.strip()
        if isinstance(raw_header, str) and raw_header.strip()
        else "Question"
    )
    options = _options(request.get("options"))
    multi_select = request.get("multi_select") is True
    return QuestionField(
        question_id=question_id,
        header=_bounded_header(header),
        prompt=prompt,
        options=options,
        multi_select=multi_select,
    )


def _options(raw_options: object) -> tuple[QuestionOption, ...]:
    if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 4:
        return ()
    options: list[QuestionOption] = []
    for item in raw_options:
        if isinstance(item, str) and item.strip():
            options.append(QuestionOption(item.strip()))
            continue
        if not isinstance(item, dict):
            return ()
        label = item.get("label")
        description = item.get("description", "")
        if not isinstance(label, str) or not label.strip() or not isinstance(description, str):
            return ()
        options.append(QuestionOption(label.strip(), description.strip()))
    return tuple(options)


def _bounded_header(value: str) -> str:
    normalized = " ".join(value.split()) or "Question"
    return normalized[:24]


def _json_preview(value: object) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered if len(rendered) <= 600 else rendered[:599] + "…"
