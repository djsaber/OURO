"""Structured question presentation and answer submission."""

from __future__ import annotations

from pydantic import JsonValue
from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static

from ouro.tui.interactions.forms import QuestionField, QuestionForm
from ouro.tui.theme import theme_for


class AskUserPanel(Static):
    """Keyboard-first renderer for a Host-projected question form."""

    can_focus = True
    BINDINGS = [
        Binding("up", "previous_option", show=False),
        Binding("down", "next_option", show=False),
        Binding("left", "previous_question", show=False),
        Binding("right", "next_question", show=False),
        Binding("space", "toggle_option", show=False),
        Binding("enter", "accept", show=False),
        Binding("1", "choose(0)", show=False),
        Binding("2", "choose(1)", show=False),
        Binding("3", "choose(2)", show=False),
        Binding("4", "choose(3)", show=False),
        Binding("5", "choose(4)", show=False),
    ]

    class Completed(Message):
        def __init__(self, answer: JsonValue) -> None:
            super().__init__()
            self.answer = answer

    class FreeformRequested(Message):
        def __init__(self, question_id: str) -> None:
            super().__init__()
            self.question_id = question_id

    def __init__(self) -> None:
        super().__init__(id="ask-user-panel")
        self.display = False
        self.disabled = True
        self._request: QuestionForm | None = None
        self._question_index = 0
        self._cursor = 0
        self._selected: set[int] = set()
        self._answers: dict[str, list[str]] = {}
        self._awaiting_freeform = False

    @property
    def awaiting_freeform(self) -> bool:
        return self._awaiting_freeform

    def show_request(self, request: QuestionForm) -> None:
        with self.app.batch_update():
            self._request = request
            self._question_index = 0
            self._cursor = 0
            self._selected.clear()
            self._answers.clear()
            self._awaiting_freeform = False
            self.disabled = False
            self.display = True
            self._show_question(0)

    def hide_panel(self) -> None:
        with self.app.batch_update():
            self.display = False
            self._request = None
            self._awaiting_freeform = False

    def resume(self) -> None:
        if self._request is not None:
            with self.app.batch_update():
                self.disabled = False
                self.display = True
                self._render_question()
                self.focus()

    def accept_freeform(self, value: str) -> None:
        if not self._awaiting_freeform or self._request is None:
            return
        normalized = value.strip()
        if not normalized:
            return
        question = self._question
        answers = [question.options[index].label for index in sorted(self._selected)]
        answers.append(normalized)
        self._awaiting_freeform = False
        self._finish_question(question, answers)

    def action_previous_option(self) -> None:
        self._move_cursor(-1)

    def action_next_option(self) -> None:
        self._move_cursor(1)

    def action_previous_question(self) -> None:
        self._move_question(-1)

    def action_next_question(self) -> None:
        self._move_question(1)

    def action_toggle_option(self) -> None:
        request = self._request
        if request is None or not self._question.multi_select:
            return
        if self._cursor >= len(self._question.options):
            self._request_freeform()
            return
        if self._cursor in self._selected:
            self._selected.remove(self._cursor)
        else:
            self._selected.add(self._cursor)
        self._render_question()

    def action_accept(self) -> None:
        request = self._request
        if request is None:
            return
        question = self._question
        if self._cursor >= len(question.options):
            self._request_freeform()
            return
        if question.multi_select:
            if not self._selected:
                self._selected.add(self._cursor)
            answers = [question.options[index].label for index in sorted(self._selected)]
        else:
            answers = [question.options[self._cursor].label]
        self._finish_question(question, answers)

    def action_choose(self, index: int) -> None:
        if self._request is None or index > len(self._question.options):
            return
        self._cursor = index
        if self._question.multi_select and index < len(self._question.options):
            self.action_toggle_option()
            return
        self.action_accept()

    @property
    def _question(self) -> QuestionField:
        assert self._request is not None
        return self._request.questions[self._question_index]

    def _move_cursor(self, offset: int) -> None:
        if self._request is None:
            return
        option_count = len(self._question.options) + 1
        self._cursor = (self._cursor + offset) % option_count
        self._render_question()

    def _request_freeform(self) -> None:
        self._awaiting_freeform = True
        self.post_message(self.FreeformRequested(self._question.question_id))

    def _finish_question(self, question: QuestionField, answers: list[str]) -> None:
        self._answers[question.question_id] = answers
        assert self._request is not None
        if self._question_index + 1 < len(self._request.questions):
            self._show_question(self._question_index + 1)
            return
        answer = self._request.encode(self._answers)
        self.disabled = True
        self.post_message(self.Completed(answer))

    def _move_question(self, offset: int) -> None:
        request = self._request
        if request is None or self._awaiting_freeform:
            return
        target = self._question_index + offset
        if not 0 <= target < len(request.questions):
            return
        if offset > 0 and self._question.question_id not in self._answers:
            return
        self._show_question(target)

    def _show_question(self, index: int) -> None:
        assert self._request is not None
        self._question_index = index
        self._cursor = 0
        self._selected.clear()
        self._awaiting_freeform = False
        question = self._question
        answers = self._answers.get(question.question_id, ())
        option_indexes = {
            option.label: option_index for option_index, option in enumerate(question.options)
        }
        self._selected.update(
            option_indexes[answer] for answer in answers if answer in option_indexes
        )
        unmatched_answer = any(answer not in option_indexes for answer in answers)
        if unmatched_answer:
            self._cursor = len(question.options)
        elif question.multi_select and self._selected:
            self._cursor = min(self._selected)
        elif answers:
            self._cursor = option_indexes.get(answers[0], 0)
        self._render_question()
        self.focus()
        if not question.options:
            self._request_freeform()

    def _render_question(self) -> None:
        request = self._request
        if request is None:
            return
        question = self._question
        total = len(request.questions)
        theme = theme_for(self)
        content = Text()
        content.append(
            f"{question.header.upper()}  {self._question_index + 1}/{total}\n",
            style=f"bold {theme.text_emphasis}",
        )
        content.append(
            f"{question.prompt}\n\n",
            style=theme.text_primary,
        )
        for index, option in enumerate(question.options):
            active = index == self._cursor
            checked = index in self._selected
            if question.multi_select:
                marker = "[x]" if checked else "[ ]"
            else:
                marker = theme.selected_choice_marker if active else theme.unselected_choice_marker
            content.append("› " if active else "  ", style=theme.accent if active else "")
            content.append(
                f"{index + 1}. {marker} {option.label}",
                style=f"bold {theme.text_primary}" if active else "",
            )
            content.append(
                f"  {option.description}\n",
                style=theme.text_lowlight,
            )
        other_index = len(question.options)
        other_active = self._cursor == other_index
        content.append("› " if other_active else "  ", style=theme.accent if other_active else "")
        content.append(
            f"{other_index + 1}. Other…",
            style=(f"bold {theme.accent_bright}" if other_active else theme.text_primary),
        )
        navigation = ("←/→ questions",) if total > 1 else ()
        controls = (
            (*navigation, "↑/↓ move", "Space toggle", "Enter continue")
            if question.multi_select
            else (*navigation, "↑/↓ move", "Enter choose")
        )
        hint = "\n\n" + theme.inline_separator.join(controls)
        content.append(hint, style=theme.text_lowlight)
        self.update(content)

    def refresh_theme(self) -> None:
        self._render_question()
