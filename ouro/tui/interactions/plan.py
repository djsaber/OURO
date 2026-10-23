"""Plan confirmation choices and decision submission."""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

from ouro.sdk.plugins.interactions import (
    PlanConfirmationAction,
    PlanConfirmationAnswer,
    PlanConfirmationRequest,
)
from ouro.tui.interactions.approval import ApprovalPanel
from ouro.tui.theme import theme_for


class PlanConfirmationPanel(ApprovalPanel):
    """Keyboard-first decision workflow for a public plan confirmation request."""

    BINDINGS = [
        *ApprovalPanel.BINDINGS,
        Binding("left,pageup", "scroll_plan_up", show=False),
        Binding("right,pagedown", "scroll_plan_down", show=False),
        Binding("escape", "back", show=False),
    ]

    class Completed(Message):
        def __init__(self, answer: JsonValue) -> None:
            super().__init__()
            self.answer = answer

    class FeedbackRequested(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            panel_id="plan-confirmation-panel",
            title_id="plan-confirmation-title",
            controls_id="plan-confirmation-controls",
        )
        self.display = False
        self.disabled = True
        self._plan_body = Static(id="plan-confirmation-body")
        self._document = VerticalScroll(
            self._plan_body,
            id="plan-confirmation-document",
            can_focus=False,
        )
        self._request: PlanConfirmationRequest | None = None
        self._stage: Literal["action", "option", "feedback"] = "action"
        self._awaiting_feedback = False

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._document
        yield self._controls

    @property
    def awaiting_feedback(self) -> bool:
        return self._awaiting_feedback

    @property
    def selected_action(self) -> PlanConfirmationAction | None:
        if self._request is None or self._stage != "action":
            return None
        return self._request.actions[self._cursor]

    def show_request(self, request: PlanConfirmationRequest) -> None:
        with self.app.batch_update():
            self._request = request
            self._stage = "action"
            self._cursor = 0
            self._awaiting_feedback = False
            self.disabled = False
            self.display = True
            self._render_panel()
            self._document.scroll_home(animate=False)
            self.focus()

    def hide_panel(self) -> None:
        with self.app.batch_update():
            self.display = False
            self._request = None
            self._stage = "action"
            self._cursor = 0
            self._awaiting_feedback = False

    def resume(self) -> None:
        if self._request is None:
            return
        with self.app.batch_update():
            self.disabled = False
            self.display = True
            self._render_panel()
            if self._awaiting_feedback:
                self.post_message(self.FeedbackRequested())
            else:
                self.focus()

    def accept_feedback(self, value: str) -> None:
        if self._request is None or not self._awaiting_feedback:
            return
        feedback = value.strip()
        if not feedback:
            return
        self._awaiting_feedback = False
        self._stage = "action"
        self._complete(PlanConfirmationAnswer(action="revise", feedback=feedback))

    def action_scroll_plan_up(self) -> None:
        self._document.scroll_page_up(animate=False)

    def action_scroll_plan_down(self) -> None:
        self._document.scroll_page_down(animate=False)

    def action_accept(self) -> None:
        request = self._request
        if request is None or self.disabled or self._stage == "feedback":
            return
        if self._stage == "option":
            selected_option = request.options[self._cursor]
            self._complete(PlanConfirmationAnswer(action="accept", option=selected_option.label))
            return

        action = request.actions[self._cursor]
        if action == "accept":
            if len(request.options) > 1:
                self._stage = "option"
                self._cursor = 0
                self._render_controls(request)
                return
            option_label = request.options[0].label if request.options else None
            self._complete(PlanConfirmationAnswer(action="accept", option=option_label))
            return
        if action == "revise":
            self._stage = "feedback"
            self._awaiting_feedback = True
            self._render_controls(request)
            self.post_message(self.FeedbackRequested())
            return
        self._complete(PlanConfirmationAnswer(action="reject"))

    def action_back(self) -> None:
        if self._request is None or self.disabled or self._stage == "action":
            return
        self._stage = "action"
        self._cursor = 0
        self._awaiting_feedback = False
        self._render_controls(self._request)
        self.focus()

    def refresh_theme(self) -> None:
        self._render_panel()

    def _choices(self) -> tuple[object, ...]:
        if self._request is None:
            return ()
        if self._stage == "action":
            return self._request.actions
        if self._stage == "option":
            return self._request.options
        return ()

    def _approval_choice_count(self) -> int:
        return len(self._choices())

    def _render_approval_controls(self) -> None:
        if self._request is not None:
            self._render_controls(self._request)

    def _complete(self, answer: PlanConfirmationAnswer) -> None:
        self.disabled = True
        self.post_message(self.Completed(answer.to_json()))

    def _render_panel(self) -> None:
        request = self._request
        if request is None:
            return
        theme = theme_for(self)
        with self.app.batch_update():
            self._render_approval_title("PLAN APPROVAL")
            self._plan_body.update(Text(request.plan, style=theme.text_primary))
            self._render_controls(request)

    def _render_controls(self, request: PlanConfirmationRequest) -> None:
        theme = theme_for(self)
        content = Text()
        if request.error:
            content.append(f"{request.error}\n\n", style=theme.failure)
        if self._stage == "action":
            content.append(
                "Choose how to respond to this plan\n\n",
                style=theme.text_lowlight,
            )
            presentations = {
                "accept": ("Accept", "Accept the proposed plan", theme.success),
                "revise": ("Revise", "Request changes and provide feedback", theme.accent),
                "reject": (
                    "Reject",
                    "Reject the proposed plan",
                    theme.danger,
                ),
            }
            for index, action in enumerate(request.actions):
                label, description, tone = presentations[action]
                self._append_approval_option(
                    content,
                    index=index,
                    label=label,
                    description=description,
                    tone=tone,
                )
            self._append_approval_hint(
                content,
                leading=("←/→ or PgUp/PgDn plan",),
            )
        elif self._stage == "option":
            content.append(
                "Choose an implementation approach\n\n",
                style=theme.text_lowlight,
            )
            for index, option in enumerate(request.options):
                self._append_approval_option(
                    content,
                    index=index,
                    label=option.label,
                    description=option.description,
                    tone=theme.accent,
                )
            self._append_approval_hint(
                content,
                leading=("←/→ or PgUp/PgDn plan",),
                trailing=("Esc back",),
            )
        else:
            content.append("REVISION FEEDBACK\n", style=f"bold {theme.text_emphasis}")
            content.append(
                "Type the requested plan changes below, then press Enter",
                style=theme.text_lowlight,
            )
        self._controls.update(content)
