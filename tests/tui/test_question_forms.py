from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from ouro.tui.question_forms import project_question_form
from ouro.tui.widgets import AskUserPanel


class _QuestionPanelApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.answers: list[object] = []

    def compose(self) -> ComposeResult:
        yield AskUserPanel()

    def on_ask_user_panel_completed(self, message: AskUserPanel.Completed) -> None:
        self.answers.append(message.answer)


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


@pytest.mark.asyncio
async def test_multi_question_panel_navigates_back_and_updates_previous_answer() -> None:
    app = _QuestionPanelApp()
    form = project_question_form(
        "PluginSurvey",
        {
            "questions": [
                {
                    "id": "first",
                    "header": "First",
                    "question": "First question?",
                    "options": ["First A", "First B"],
                },
                {
                    "id": "second",
                    "header": "Second",
                    "question": "Second question?",
                    "options": ["Second A", "Second B"],
                },
                {
                    "id": "third",
                    "header": "Third",
                    "question": "Third question?",
                    "options": ["Third A", "Third B"],
                },
            ]
        },
    )

    async with app.run_test(size=(60, 18)) as pilot:
        panel = app.query_one(AskUserPanel)
        panel.show_request(form)
        await pilot.pause()

        assert panel.render().plain.startswith("FIRST  1/3")
        assert "←/→ questions • ↑/↓ move • Enter choose" in panel.render().plain

        await pilot.press("right")
        assert panel.render().plain.startswith("FIRST  1/3")

        await pilot.press("2")
        assert panel.render().plain.startswith("SECOND  2/3")

        await pilot.press("left")
        first_b = next(line for line in panel.render().plain.splitlines() if "First B" in line)
        assert first_b.startswith("› 2. ●")

        await pilot.press("up", "enter")
        assert panel.render().plain.startswith("SECOND  2/3")

        await pilot.press("right")
        assert panel.render().plain.startswith("SECOND  2/3")

        await pilot.press("2")
        assert panel.render().plain.startswith("THIRD  3/3")

        await pilot.press("left")
        second_b = next(line for line in panel.render().plain.splitlines() if "Second B" in line)
        assert second_b.startswith("› 2. ●")

        await pilot.press("right", "1")
        await pilot.pause()

        assert app.answers == [
            {
                "answers": {
                    "first": ["First A"],
                    "second": ["Second B"],
                    "third": ["Third A"],
                }
            }
        ]
