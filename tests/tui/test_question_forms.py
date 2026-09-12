from __future__ import annotations

from ouro.tui.question_forms import project_question_form


def test_builtin_ask_user_projects_to_structured_answer_mapping() -> None:
    form = project_question_form(
        "AskUser",
        {
            "type": "ask_user",
            "questions": [
                {
                    "id": "database",
                    "header": "Database",
                    "question": "Which database should OURO use?",
                    "options": [
                        {
                            "label": "SQLite (Recommended)",
                            "description": "Keep local setup simple.",
                        },
                        {
                            "label": "Postgres",
                            "description": "Use a shared service.",
                        },
                    ],
                    "multi_select": False,
                }
            ],
        },
    )

    assert form.questions[0].question_id == "database"
    assert form.questions[0].options[1].label == "Postgres"
    assert form.encode({"database": ["Postgres"]}) == {"answers": {"database": ["Postgres"]}}


def test_plugin_question_list_uses_same_form_without_builtin_tool_coupling() -> None:
    form = project_question_form(
        "PluginSurvey",
        {
            "questions": [
                {
                    "id": "targets",
                    "header": "Targets",
                    "prompt": "Choose deployment targets",
                    "options": ["Linux", "Windows"],
                    "multi_select": True,
                }
            ]
        },
    )

    assert form.questions[0].multi_select is True
    assert form.encode({"targets": ["Linux", "Windows"]}) == {
        "answers": {"targets": ["Linux", "Windows"]}
    }


def test_single_plugin_question_returns_the_selected_value() -> None:
    form = project_question_form(
        "PluginQuestion",
        {
            "question": "How should the plugin continue?",
            "options": [
                {"label": "Safely", "description": "Use guarded defaults."},
                {"label": "Quickly", "description": "Prefer lower latency."},
            ],
        },
    )

    assert form.encode({"answer": ["Safely"]}) == "Safely"


def test_opaque_plugin_request_degrades_to_bounded_freeform_input() -> None:
    form = project_question_form("OpaquePlugin", {"type": "custom", "payload": [1, 2]})

    assert form.questions[0].options == ()
    assert '"type": "custom"' in form.questions[0].prompt
    assert form.encode({"answer": ["continue"]}) == "continue"
