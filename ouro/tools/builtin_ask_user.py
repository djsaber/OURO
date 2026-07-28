from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ouro.core.base import OuroModel
from ouro.tools.base import Tool, ToolDefinition, ToolInputRequest, ToolInvocation

ASK_USER_TOOL_NAME = "AskUser"
ASK_USER_REQUEST_TYPE = "ask_user"

_ASK_USER_DESCRIPTION = """\
Ask the user for information that is necessary to continue and cannot be inferred or discovered
from the available context. Use this tool only when the answer will materially change the work;
do not interrupt the user for trivial choices or questions you can resolve yourself. Prefer one
question and group closely related questions into one call, up to three. Provide 2-4 distinct,
actionable options per question. Put the recommended option first and suffix its label with
'(Recommended)'. Do not add an 'Other' option because the Host always provides free-form input.
Set multi_select only when the choices are independent and more than one may apply.
"""


class AskUserOption(OuroModel):
    label: str = Field(
        min_length=1,
        description=(
            "Concise user-facing label (1-5 words). Put a recommended option first and suffix "
            "its label with '(Recommended)'."
        ),
    )
    description: str = Field(
        min_length=1,
        description="One short sentence explaining the impact or trade-off of this option.",
    )

    @field_validator("label", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized


class AskUserQuestion(OuroModel):
    id: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Stable lower-case snake_case identifier used to map the answer.",
    )
    header: str = Field(
        min_length=1,
        max_length=12,
        description="Short category label shown in the UI (12 or fewer characters).",
    )
    question: str = Field(
        min_length=1,
        description="A clear, specific question shown to the user.",
    )
    options: tuple[AskUserOption, ...] = Field(
        min_length=2,
        max_length=4,
        description=(
            "Two to four meaningful choices. Do not include an 'Other' option; the Host adds "
            "free-form input automatically."
        ),
    )
    multi_select: bool = Field(
        default=False,
        description="Allow multiple answers when the choices are independent.",
    )

    @field_validator("header", "question")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_distinct_options(self) -> Self:
        labels = [option.label.casefold() for option in self.options]
        if len(labels) != len(set(labels)):
            raise ValueError("option labels must be unique within a question")
        return self


class AskUserArguments(OuroModel):
    questions: tuple[AskUserQuestion, ...] = Field(
        min_length=1,
        max_length=3,
        description="Related questions to show together. Prefer one and do not exceed three.",
    )

    @model_validator(mode="after")
    def validate_distinct_question_ids(self) -> Self:
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return self


class AskUserRequest(AskUserArguments):
    type: Literal["ask_user"] = "ask_user"

    def to_json(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


NonEmptyAnswers = Annotated[list[str], Field(min_length=1)]


class AskUserAnswer(OuroModel):
    answers: dict[str, NonEmptyAnswers]

    def to_json(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


def builtin_ask_user_tool() -> Tool:
    return Tool(
        definition=ToolDefinition(
            name=ASK_USER_TOOL_NAME,
            description=_ASK_USER_DESCRIPTION,
            parameters_schema=AskUserArguments.model_json_schema(),
            permission_mode="auto",
            execution_mode="sequential",
            effect="read_only",
            batch_behavior="exclusive",
            kind="user_input",
        ),
        handler=_ask_user,
    )


def parse_builtin_ask_user_request(
    tool_name: str,
    data: object,
) -> AskUserRequest | None:
    """Parse only the Host request emitted by OURO's built-in AskUser tool."""

    if (
        tool_name != ASK_USER_TOOL_NAME
        or not isinstance(data, dict)
        or data.get("type") != ASK_USER_REQUEST_TYPE
    ):
        return None
    return AskUserRequest.model_validate(data)


def _ask_user(invocation: ToolInvocation) -> ToolInputRequest:
    arguments = AskUserArguments.model_validate(invocation.arguments)
    request = AskUserRequest(questions=arguments.questions)
    return ToolInputRequest(data=request.to_json())


__all__ = [
    "ASK_USER_REQUEST_TYPE",
    "ASK_USER_TOOL_NAME",
    "AskUserAnswer",
    "AskUserArguments",
    "AskUserOption",
    "AskUserQuestion",
    "AskUserRequest",
    "builtin_ask_user_tool",
    "parse_builtin_ask_user_request",
]
