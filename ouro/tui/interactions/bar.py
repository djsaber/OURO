"""Active interaction presentation and panel switching."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Static

from ouro.tui.interactions.approval import ApprovalPanel
from ouro.tui.state import PendingInteraction
from ouro.tui.theme import theme_for


class InteractionBar(ApprovalPanel):
    """Tool approval projected through the shared approval panel."""

    class Decision(Message):
        def __init__(self, interaction: PendingInteraction, approved: bool) -> None:
            super().__init__()
            self.interaction = interaction
            self.approved = approved

    def __init__(self) -> None:
        super().__init__(
            panel_id="interaction-bar",
            title_id="interaction-title",
            controls_id="interaction-controls",
        )
        self._summary = Static(id="interaction-summary")
        self.display = False
        self.disabled = True
        self._interaction: PendingInteraction | None = None
        self._identity: tuple[str, str] | None = None
        self._sequence_owner: tuple[str | None, str] | None = None
        self._sequence_position = 0
        self._sequence_total = 0

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._summary
        yield self._controls

    @property
    def selected_approved(self) -> bool:
        return self._cursor == 0

    def show_interaction(
        self,
        interaction: PendingInteraction | None,
        *,
        pending_count: int = 1,
    ) -> None:
        with self.app.batch_update():
            if interaction is None or interaction.kind != "approval":
                self.display = False
                self._interaction = None
                self._identity = None
                self._sequence_owner = None
                self._sequence_position = 0
                self._sequence_total = 0
                self._cursor = 0
                return
            identity = (interaction.run_id, interaction.request_id or interaction.tool_call_id)
            if identity != self._identity:
                self._cursor = 0
                owner = (interaction.session_id, interaction.run_id)
                if self._identity is None or owner != self._sequence_owner:
                    self._sequence_owner = owner
                    self._sequence_position = 1
                    self._sequence_total = max(1, pending_count)
                else:
                    self._sequence_position += 1
                    self._sequence_total = max(
                        self._sequence_total,
                        self._sequence_position + max(1, pending_count) - 1,
                    )
            self._interaction = interaction
            self._identity = identity
            self.disabled = False
            self.display = True
            self._render_approval()

    def update_pending_count(self, pending_count: int) -> None:
        if self._interaction is None:
            return
        total = max(
            self._sequence_total,
            self._sequence_position + max(1, pending_count) - 1,
        )
        if total != self._sequence_total:
            self._sequence_total = total
            self._render_approval()

    def resume(self) -> None:
        if self._interaction is None:
            return
        with self.app.batch_update():
            self.disabled = False
            self.display = True
            self._render_approval()
            self.focus()

    def refresh_theme(self) -> None:
        self._render_approval()

    def action_accept(self) -> None:
        self._decide(self.selected_approved)

    def _approval_choice_count(self) -> int:
        return 2 if self._interaction is not None else 0

    def _render_approval_controls(self) -> None:
        self._render_approval()

    def _decide(self, approved: bool) -> None:
        interaction = self._interaction
        if interaction is None or self.disabled:
            return
        self.disabled = True
        self.post_message(self.Decision(interaction, approved))

    def _render_approval(self) -> None:
        interaction = self._interaction
        if interaction is None:
            return
        theme = theme_for(self)
        title = (
            f"APPROVAL  {self._sequence_position}/{self._sequence_total}"
            if self._sequence_total > 1
            else "APPROVAL"
        )
        self._render_approval_title(title)
        summary = Text()
        if interaction.is_subagent:
            summary.append(
                f"Subagent {interaction.agent_name or interaction.delegation_id}\n",
                style=theme.text_lowlight,
            )
        summary.append(
            f"{interaction.tool_name} wants to run",
            style=f"bold {theme.text_primary}",
        )
        self._summary.update(summary)
        content = Text()
        options = (
            ("Allow", "Run this tool", theme.success),
            ("Deny", "Reject this tool", theme.danger),
        )
        for index, (label, description, tone) in enumerate(options):
            self._append_approval_option(
                content,
                index=index,
                label=label,
                description=description,
                tone=tone,
            )
        self._append_approval_hint(content)
        self._controls.update(content)
        self.call_after_refresh(self._scroll_approval_selection_into_view)
