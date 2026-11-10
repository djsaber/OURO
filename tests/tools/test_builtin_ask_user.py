import pytest

from ouro.tools.builtin.ask_user import (
    AskUserArguments,
    AskUserRequest,
    builtin_ask_user_tool,
    parse_builtin_ask_user_request,
)


def _arguments() -> dict[str, object]:
    return {
        "questions": [
            {
                "id": "database",
                "header": "Database",
                "question": "Which database should the service use?",
                "options": [
                    {
                        "label": "SQLite (Recommended)",
                        "description": "Keep local setup simple and dependency-free.",
                    },
                    {
                        "label": "PostgreSQL",
                        "description": "Support concurrent production workloads.",
                    },
                ],
            }
        ]
    }


def test_ask_user_definition_exposes_bounded_structured_contract() -> None:
    definition = builtin_ask_user_tool().definition
    schema = definition.parameters_schema

    assert schema["required"] == ["questions"]
    assert "additionalProperties" not in schema
    assert schema["properties"]["questions"]["minItems"] == 1
    assert schema["properties"]["questions"]["maxItems"] == 3

    question_schema = schema["$defs"]["AskUserQuestion"]
    assert question_schema["required"] == ["id", "header", "question", "options"]
    assert question_schema["properties"]["options"]["minItems"] == 2
    assert question_schema["properties"]["options"]["maxItems"] == 4


def test_ask_user_contract_rejects_duplicate_question_ids_and_option_labels() -> None:
    duplicate_questions = _arguments()["questions"] * 2  # type: ignore[operator]
    with pytest.raises(ValueError, match="question ids must be unique"):
        AskUserArguments.model_validate({"questions": duplicate_questions})

    arguments = _arguments()
    question = arguments["questions"][0]  # type: ignore[index]
    question["options"][1]["label"] = "sqlite (recommended)"  # type: ignore[index]
    with pytest.raises(ValueError, match="option labels must be unique"):
        AskUserArguments.model_validate(arguments)


def test_parse_builtin_ask_user_request_requires_tool_and_discriminator() -> None:
    request = AskUserRequest.model_validate({"type": "ask_user", **_arguments()})
    data = request.to_json()

    assert parse_builtin_ask_user_request("AskUser", data) == request
    assert parse_builtin_ask_user_request("PluginQuestion", data) is None
    assert parse_builtin_ask_user_request("AskUser", _arguments()) is None
