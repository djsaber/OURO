"""Presentation of queued user input and queue actions."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ouro.application.inputs.queue import QueuedUserInputView
from ouro.tui.composer.attachment_editor import file_count_label
from ouro.tui.composer.popup import _trailing_badge
from ouro.tui.theme import theme_for


class QueuedInputPanel(VerticalScroll):
    """Show inputs waiting on an active Run; retain all pending inputs for withdrawal."""

    def __init__(self) -> None:
        super().__init__(id="queued-input-panel")
        self.display = False
        self._content = Static(id="queued-input-content")
        self._inputs: tuple[QueuedUserInputView, ...] = ()

    def compose(self) -> ComposeResult:
        yield self._content

    @property
    def inputs(self) -> tuple[QueuedUserInputView, ...]:
        return self._inputs

    def refresh_theme(self) -> None:
        self.set_inputs(self._inputs)

    def on_resize(self, event: events.Resize) -> None:
        self.set_inputs(self._inputs)

    def set_inputs(self, inputs: tuple[QueuedUserInputView, ...]) -> None:
        with self.app.batch_update():
            self._inputs = inputs
            inputs = tuple(item for item in inputs if item.waiting_on_run_id is not None)
            if not inputs:
                self.display = False
                return
            self.display = True
            theme = theme_for(self)
            content = Text()
            for index, item in enumerate(inputs, start=1):
                label = "Follow-up" if item.mode == "follow_up" else "Guide"
                tone = theme.accent if item.mode == "follow_up" else theme.accent_bright
                prompt = " ↵ ".join(line.strip() for line in item.prompt.splitlines())
                if item.skills:
                    prompt = " ".join(skill.label for skill in item.skills) + f" {prompt}"
                row = Text(f"{index}. {label}", style=tone)
                row.append(
                    theme.inline_separator,
                    style=theme.text_lowlight,
                )
                row.append(prompt, style=theme.text_emphasis)
                badge = file_count_label(item.attachments, item.resource_refs)
                if badge:
                    row = _trailing_badge(
                        row, badge, self.scrollable_content_region.width, theme.accent
                    )
                content.append_text(row)
                if index < len(inputs):
                    content.append("\n")
            self._content.update(content)
            self.scroll_end(animate=False)
