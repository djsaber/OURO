from collections.abc import Iterator

from ouro.cli.ask_user import AskUserControl, collect_ask_user_answers
from ouro.tools.builtin_ask_user import AskUserRequest


def _request(*, multi_select: bool = False, two_questions: bool = False) -> AskUserRequest:
    questions: list[dict[str, object]] = [
        {
            "id": "database",
            "header": "Database",
            "question": "Which database should the service use?",
            "options": [
                {
                    "label": "SQLite (Recommended)",
                    "description": "Keep local setup simple.",
                },
                {
                    "label": "PostgreSQL",
                    "description": "Support concurrent workloads.",
                },
            ],
            "multi_select": multi_select,
        }
    ]
    if two_questions:
        questions.append(
            {
                "id": "interface",
                "header": "Interface",
                "question": "Which interface should be included?",
                "options": [
                    {"label": "CLI", "description": "Expose terminal commands."},
                    {"label": "HTTP", "description": "Expose a web API."},
                ],
            }
        )
    return AskUserRequest.model_validate({"type": "ask_user", "questions": questions})


def _input(values: Iterator[str]):
    return lambda _prompt: next(values)


def test_collect_single_select_returns_stable_id_and_canonical_label() -> None:
    outputs: list[str] = []

    answer = collect_ask_user_answers(
        _request(),
        input_func=_input(iter(["2"])),
        output_func=outputs.append,
    )

    assert answer == {"answers": {"database": ["PostgreSQL"]}}
    assert outputs == [
        "Question · Database\n"
        "Which database should the service use?\n\n"
        "  1. SQLite (Recommended)\n"
        "     Keep local setup simple.\n"
        "  2. PostgreSQL\n"
        "     Support concurrent workloads.\n"
        "  3. Other\n"
        "     Type a custom answer."
    ]


def test_collect_multi_select_combines_options_and_other_without_duplicates() -> None:
    answer = collect_ask_user_answers(
        _request(multi_select=True),
        input_func=_input(iter(["1, 1, 3", "CockroachDB"])),
        output_func=lambda _text: None,
    )

    assert answer == {
        "answers": {"database": ["SQLite (Recommended)", "CockroachDB"]}
    }


def test_collect_questions_uses_prefetched_first_answer_and_question_ids() -> None:
    outputs: list[str] = []

    answer = collect_ask_user_answers(
        _request(two_questions=True),
        input_func=_input(iter(["HTTP"])),
        output_func=outputs.append,
        initial_response="SQLite (Recommended)",
        first_question_rendered=True,
    )

    assert answer == {
        "answers": {
            "database": ["SQLite (Recommended)"],
            "interface": ["HTTP"],
        }
    }
    assert len(outputs) == 1
    assert outputs[0].startswith("Question 2/2 · Interface")


def test_collect_reprompts_after_empty_answer() -> None:
    outputs: list[str] = []

    answer = collect_ask_user_answers(
        _request(),
        input_func=_input(iter(["", "1"])),
        output_func=outputs.append,
    )

    assert answer == {"answers": {"database": ["SQLite (Recommended)"]}}
    assert "Answer cannot be empty." in outputs


def test_collect_supports_abort_and_eof() -> None:
    aborted = collect_ask_user_answers(
        _request(),
        input_func=_input(iter(["/abort"])),
        output_func=lambda _text: None,
    )

    def eof(_prompt: str) -> str:
        raise EOFError

    ended = collect_ask_user_answers(
        _request(),
        input_func=eof,
        output_func=lambda _text: None,
    )

    assert aborted is AskUserControl.ABORT
    assert ended is AskUserControl.EOF
