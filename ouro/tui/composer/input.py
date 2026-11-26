"""Multiline input composition and keyboard interaction."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from rich.style import Style as RichStyle
from textual import events, work
from textual.binding import Binding
from textual.color import Color
from textual.content import Content
from textual.message import Message
from textual.strip import Strip
from textual.style import Style
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme

from ouro.core.input_content import InputContent, InputFile, InputSkillReference
from ouro.core.session_controls import SessionInputMode
from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.composer.attachment_paths import PASTED_TEXT_ATTACHMENT_THRESHOLD
from ouro.tui.composer.element_editor import ElementTextArea
from ouro.tui.input_presentation import ContextElement
from ouro.tui.theme import DEFAULT_TUI_THEME, theme_for

COMPOSER_TEXT_MIN_HEIGHT = 1


COMPOSER_TEXT_MAX_HEIGHT = 3


COMPOSER_FRAME_HEIGHT = 2


COMPOSER_MIN_HEIGHT = COMPOSER_TEXT_MIN_HEIGHT + COMPOSER_FRAME_HEIGHT


COMPOSER_MAX_HEIGHT = COMPOSER_TEXT_MAX_HEIGHT + COMPOSER_FRAME_HEIGHT


_PlaceholderTone = Literal["text", "key"]


_PlaceholderPart = tuple[str, _PlaceholderTone]


class Composer(ElementTextArea):
    DEFAULT_PLACEHOLDER = "Ask anything..."
    ACTIVE_RUN_PLACEHOLDER = DEFAULT_TUI_THEME.inline_separator.join(
        ("Enter queues a follow-up", "Ctrl+G guides the active run…")
    )
    QUESTION_PLACEHOLDER = "Choose an option above…"
    FREEFORM_PLACEHOLDER = "Type another answer…"
    PLAN_FEEDBACK_PLACEHOLDER = "Describe the requested plan changes…"
    BINDINGS = [
        Binding("enter", "submit", "Send", show=False, priority=True),
        Binding("ctrl+g", "submit_steer", "Guide", show=False, priority=True),
        Binding("up", "history_previous", show=False, priority=True),
        Binding("down", "history_next", show=False, priority=True),
        Binding("escape", "dismiss_command", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(
            self,
            content: InputContent,
            mode: SessionInputMode,
            context_element: ContextElement | None = None,
        ) -> None:
            super().__init__()
            self.content = content
            self.mode = mode
            self.context_element = context_element

    class AttachmentRequested(Message):
        def __init__(self, paste: str | None = None) -> None:
            super().__init__()
            self.paste = paste

    class AttachmentToggled(Message):
        pass

    class CommandSubmitted(Message):
        def __init__(
            self, text: str, *, command_text: str, draft: str, session_id: str | None
        ) -> None:
            super().__init__()
            self.text = text
            self.command_text = command_text
            self.draft = draft
            self.session_id = session_id

    class CommandNavigated(Message):
        def __init__(self, offset: int) -> None:
            super().__init__()
            self.offset = offset

    class CommandDismissed(Message):
        pass

    def __init__(
        self,
        *,
        clipboard_reader: Callable[[], str] = read_system_clipboard,
    ) -> None:
        super().__init__(
            id="composer",
            placeholder=self.DEFAULT_PLACEHOLDER,
            soft_wrap=True,
            show_line_numbers=False,
            compact=True,
        )
        self.register_theme(
            TextAreaTheme(
                "ouro-composer",
                selection_style=RichStyle(reverse=True),
                cursor_line_style=RichStyle(),
            )
        )
        self.theme = "ouro-composer"
        self._history: list[InputContent] = []
        self._history_index = 0
        self._clipboard_reader = clipboard_reader
        self._observed_text = ""
        self._command_draft: str | None = None
        self._attachment_draft: InputContent | None = None
        self.reference_command = "@"
        self._attachment_selection = self.selection
        self._suggestion_resolver: Callable[[], str] | None = None
        self._command_matcher: Callable[[], bool] | None = None
        self.command_session_id: Callable[[], str | None] = lambda: self._history_session_id
        self._placeholder_parts: tuple[_PlaceholderPart, ...] = ()
        self._placeholder_kind: Literal["active_run", "subagent"] | None = None
        # Some terminals emit Paste alongside the right-click mouse event.
        self._right_click_paste_pending = False
        self._right_click_paste_handled = False
        self.attachments_enabled = True
        self.import_pending = False
        self.submission_pending = False
        self._history_session_id: str | None = None

    def bind_history(self, session_id: str | None) -> None:
        if session_id != self._history_session_id:
            self._history.clear()
            self._history_index = 0
            self._history_session_id = session_id

    @property
    def projected_height(self) -> int:
        """The outer height Textual resolves for the current wrapped document.

        ``height: auto`` on a ``ScrollView`` resolves through the virtual size,
        so the wrapped document height bounded by the composer's text limits is
        the same value the box model arrives at. Reading it here lets adjacent
        panels re-bound in the layout pass that grows the composer.
        """

        text_height = min(
            COMPOSER_TEXT_MAX_HEIGHT,
            max(COMPOSER_TEXT_MIN_HEIGHT, self.wrapped_document.height),
        )
        return text_height + COMPOSER_FRAME_HEIGHT

    def set_suggestion_resolver(self, resolver: Callable[[], str]) -> None:
        """Resolve inline suggestions synchronously with text edits."""

        self._suggestion_resolver = resolver
        self.update_suggestion()

    def set_command_matcher(self, matcher: Callable[[], bool]) -> None:
        """Let the host distinguish command candidates from ordinary input."""

        self._command_matcher = matcher

    def _matches_command(self) -> bool:
        return self._command_matcher is None or self._command_matcher()

    def set_plain_placeholder(self, placeholder: str) -> None:
        if not self._placeholder_parts and self.placeholder == placeholder:
            return
        self._placeholder_parts = ()
        self._placeholder_kind = None
        self.placeholder = placeholder

    def set_active_run_placeholder(self) -> None:
        self._set_styled_placeholder("active_run")

    def set_subagent_placeholder(self) -> None:
        self._set_styled_placeholder("subagent")

    def _set_styled_placeholder(
        self,
        kind: Literal["active_run", "subagent"],
        *,
        force: bool = False,
    ) -> None:
        theme = theme_for(self)
        if kind == "active_run":
            parts: tuple[_PlaceholderPart, ...] = (
                ("Enter", "key"),
                (" queues a follow-up", "text"),
                (theme.inline_separator, "text"),
                ("Ctrl+G", "key"),
                (" guides the active run…", "text"),
            )
        else:
            parts = (
                ("Subagent view is read-only", "text"),
                (theme.inline_separator, "text"),
                ("Alt+↑", "key"),
                (" main", "text"),
                (theme.inline_separator, "text"),
                ("Alt+←/→", "key"),
                (" switch", "text"),
            )
        if not force and self._placeholder_parts == parts:
            return
        self._placeholder_kind = kind
        self._placeholder_parts = parts
        self._refresh_styled_placeholder()

    def _refresh_styled_placeholder(self) -> None:
        placeholder = self._render_styled_placeholder(self._placeholder_parts)
        # Content equality ignores span styling, so replace the reactive value
        # explicitly before notifying Textual about the visual mutation.
        self.set_reactive(TextArea.placeholder, placeholder)
        self.mutate_reactive(TextArea.placeholder)

    def _render_styled_placeholder(
        self,
        parts: tuple[_PlaceholderPart, ...],
    ) -> Content:
        theme = theme_for(self)
        key_style = Style(
            foreground=Color.parse(theme.accent_bright),
            bold=False,
        )
        lowlight_style = Style(foreground=Color.parse(theme.text_lowlight))
        styles = {
            "text": lowlight_style,
            "key": key_style,
        }
        return Content.assemble(
            *((text, styles[tone]) for text, tone in parts),
        )

    def refresh_theme(self) -> None:
        if self._placeholder_kind is not None:
            self._set_styled_placeholder(self._placeholder_kind, force=True)

    def render_line(self, y: int) -> Strip:
        if isinstance(self.placeholder, Content) and (
            not self.text or self.has_class("read-only-session")
        ):
            lines = self.placeholder.wrap(self.content_size.width)
            if y < len(lines):
                content = lines[y]
                if self._draw_cursor and y == 0:
                    theme = self._theme
                    cursor_style = theme.cursor_style if theme is not None else None
                    if cursor_style is not None:
                        content = content.stylize(
                            Style.from_rich_style(cursor_style),
                            0,
                            1,
                        )
                return Strip(
                    content.render_segments(self.visual_style),
                    content.cell_length,
                )
            if self.has_class("read-only-session"):
                return Strip.blank(self.content_size.width, self.visual_style)
        return super().render_line(y)

    def update_suggestion(self) -> None:
        """Keep the suggestion aligned with the document before it is redrawn."""

        if self._suggestion_resolver is not None:
            self.suggestion = self._suggestion_resolver()

    def observe_command_change(self) -> tuple[str, str] | None:
        """Return the active command fragment and the draft behind it."""

        value = self.input_text
        previous = self._observed_text
        draft = self._command_draft
        if draft is None and previous:
            inserted = self._prefix_before_draft(previous)
            if inserted is not None and inserted.startswith("/") and "\n" not in inserted:
                draft = previous
                self._command_draft = draft
        self._observed_text = value
        return self.command_context()

    def _prefix_before_draft(self, draft: str) -> str | None:
        body = self.input_text
        return body[: len(body) - len(draft)] if body.endswith(draft) else None

    def command_context(self) -> tuple[str, str] | None:
        """Return command text separately from any pre-existing draft."""

        draft = self._command_draft
        if draft is not None:
            command_text = self._prefix_before_draft(draft)
            if (
                command_text is not None
                and (
                    command_text.startswith("/")
                    or (self.attachment_menu_open and command_text.startswith("@"))
                )
                and "\n" not in command_text
            ):
                return command_text, draft
            self._command_draft = None
        text = self.input_text
        if text.startswith("/") and "\n" not in text:
            return text, ""
        if self.attachment_menu_open and text.startswith("@") and "\n" not in text:
            return text, ""
        return None

    @property
    def attachment_menu_open(self) -> bool:
        return self._attachment_draft is not None

    def begin_attachment_menu(self) -> None:
        if self.attachment_menu_open:
            return
        self._attachment_draft = self.input_content
        self._attachment_selection = self.selection
        self.set_attachment_query("")

    def set_attachment_query(self, value: str) -> None:
        prefix = self.reference_command + (" " if self.reference_command != "@" else "")
        if self._attachment_draft is not None:
            self.load_command(prefix + value, draft=self._attachment_draft.text)

    def select_reference_command(self, command: str) -> None:
        self.reference_command = command
        self.set_attachment_query("")

    def finish_attachment_menu(self) -> None:
        content = self._attachment_draft
        if content is None:
            return
        self._attachment_draft = None
        self.load_input(content, context_element=self.context_element)
        self.selection = self._attachment_selection
        self.reference_command = "@"

    @property
    def draft_content(self) -> InputContent:
        if self._attachment_draft is not None:
            return self._attachment_draft
        context = self.command_context()
        content = self.input_content
        if context is None or (
            not context[1] and (not self.element_spans or not self._matches_command())
        ):
            return content
        prefix = len(context[0])
        return InputContent(
            text=content.text[prefix:],
            attachments=tuple(
                item.model_copy(update={"text_offset": max(0, item.text_offset - prefix)})
                for item in content.attachments
            ),
            skills=tuple(
                item.model_copy(update={"text_offset": max(0, item.text_offset - prefix)})
                for item in content.skills
            ),
        )

    def command_cursor_at_end(self) -> bool:
        """Return whether a suggestion may be inserted at the command boundary."""

        context = self.command_context()
        return bool(
            context is not None
            and self.selection.is_empty
            and self.cursor_location == (0, len(context[0]))
        )

    def on_text_area_selection_changed(self, message: TextArea.SelectionChanged) -> None:
        if message.text_area is self and not self.command_cursor_at_end():
            self.suggestion = ""

    def load_input(
        self, content: InputContent, *, context_element: ContextElement | None = None
    ) -> None:
        self._command_draft = None
        super().load_input(content, context_element=context_element)
        self._observed_text = self.input_text

    def _content_behind_command(self, draft: str) -> InputContent:
        content = self.draft_content
        return content if content.text == draft else InputContent(text=draft)

    def load_command(self, command_text: str, *, draft: str = "") -> None:
        """Edit a command while keeping an existing user draft intact."""

        content = self._content_behind_command(draft)
        self.load_input(
            InputContent(
                text=command_text + content.text,
                attachments=tuple(
                    item.model_copy(update={"text_offset": item.text_offset + len(command_text)})
                    for item in content.attachments
                ),
                skills=tuple(
                    item.model_copy(update={"text_offset": item.text_offset + len(command_text)})
                    for item in content.skills
                ),
            ),
            context_element=self.context_element,
        )
        self._command_draft = draft or None
        self.move_cursor((0, len(command_text)))

    def restore_command_draft(self, draft: str) -> None:
        """Finish a transient command and restore its original user draft."""

        content = self._content_behind_command(draft)
        self.load_input(content, context_element=self.context_element)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if self.import_pending:
            event.prevent_default()
            event.stop()
            return
        if event.button != 3:
            return
        event.prevent_default()
        event.stop()
        self._right_click_paste_pending = True
        self._right_click_paste_handled = False
        self.focus()

    def on_click(self, event: events.Click) -> None:
        if event.button != 3:
            return
        event.prevent_default()
        event.stop()
        if not self._right_click_paste_pending:
            return
        handled = self._right_click_paste_handled
        self._right_click_paste_pending = False
        if not handled:
            self._paste_system_clipboard()

    def on_paste(self, event: events.Paste) -> None:
        if self.consume_paste(event):
            self._insert_paste(event.text)

    def consume_paste(self, event: events.Paste) -> bool:
        """Claim a paste once, including the terminal's right-click paste event."""

        event.prevent_default()
        event.stop()
        if self._right_click_paste_pending:
            if self._right_click_paste_handled:
                return False
            self._right_click_paste_handled = True
        return True

    @work(group="composer-clipboard")
    async def _paste_system_clipboard(self) -> None:
        try:
            text = await asyncio.to_thread(self._clipboard_reader)
        except ClipboardReadError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
        self._insert_paste(text)

    def _insert_paste(self, text: str) -> None:
        if not text or self.read_only:
            return
        if self.import_pending:
            return
        if self.attachment_menu_open:
            self.insert_plain_paste(text)
            return
        if self.attachments_enabled and (
            len(text) > PASTED_TEXT_ATTACHMENT_THRESHOLD
            or text.lstrip().startswith(("/", '"', "'", "file:"))
            or (len(text) > 2 and text[1:3] in (":\\", ":/"))
        ):
            self.import_pending = True
            self.post_message(self.AttachmentRequested(text))
            return
        self.insert_plain_paste(text)

    def insert_plain_paste(self, text: str) -> None:
        result = self.replace(
            text,
            *self.selection,
            maintain_selection_offset=False,
        )
        self.move_cursor(result.end_location)

    async def _on_key(self, event: events.Key) -> None:
        if self.import_pending:
            event.stop()
            event.prevent_default()
            return
        context = self.command_context()
        if (
            self.attachment_menu_open
            and context is not None
            and " " in context[0]
            and event.character == " "
            and self._matches_command()
        ):
            event.stop()
            event.prevent_default()
            self.post_message(self.AttachmentToggled())
            return
        position = self.plain_document.get_index_from_location(self.selection.end)
        if event.character == "/" and position == 0 and self.selection.is_empty and context is None:
            self._command_draft = self.input_text or None
        if (
            event.character == "@"
            and self.attachments_enabled
            and not self.read_only
            and position == 0
            and self.command_context() is None
            and not self.attachment_menu_open
        ):
            event.stop()
            event.prevent_default()
            self.begin_attachment_menu()
            self.post_message(self.AttachmentRequested())
            return
        await super()._on_key(event)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if self.import_pending:
            return False
        return super().check_action(action, parameters)

    def insert_attachments(self, attachments: tuple[InputFile, ...]) -> None:
        if not attachments or self.read_only:
            return
        if len(self.input_content.attachments) + len(attachments) > 16:
            raise ValueError("A message can contain at most 16 attachments")
        self.insert_elements(attachments)

    def insert_skills(self, skills: tuple[InputSkillReference, ...]) -> None:
        existing = {item.name for item in self.input_content.skills}
        self.insert_elements(tuple(item for item in skills if item.name not in existing))

    def action_submit(self) -> None:
        self._submit("follow_up")

    def action_submit_steer(self) -> None:
        self._submit("steer")

    def _submit(self, mode: SessionInputMode) -> None:
        content = self.input_content
        if content.is_empty or self.import_pending:
            return
        context = self.command_context()
        if context is not None and self._matches_command():
            command_text, draft = context
            self.post_message(
                self.CommandSubmitted(
                    content.text,
                    command_text=command_text.strip(),
                    draft=draft,
                    session_id=self.command_session_id(),
                )
            )
            return
        if self.submission_pending:
            return
        if context is not None and self.attachment_menu_open:
            self.finish_attachment_menu()
            self.insert_plain_paste(context[0])
            content = self.input_content
        self._command_draft = None
        self._history.append(content)
        self._history_index = len(self._history)
        self.post_message(self.Submitted(content, mode, self.context_element))
        self.clear()

    def action_cursor_right(self, select: bool = False) -> None:
        if self.suggestion and not self.command_cursor_at_end():
            self.suggestion = ""
        super().action_cursor_right(select)

    def action_history_previous(self) -> None:
        if self.command_context() is not None:
            self.post_message(self.CommandNavigated(-1))
            return
        if "\n" in self.input_text or not self._can_browse_history():
            self.action_cursor_up()
            return
        if self._history_index > 0:
            self._history_index -= 1
            self.load_input(
                self._history[self._history_index], context_element=self.context_element
            )

    def action_history_next(self) -> None:
        if self.command_context() is not None:
            self.post_message(self.CommandNavigated(1))
            return
        if "\n" in self.input_text or not self._can_browse_history():
            self.action_cursor_down()
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.load_input(
                self._history[self._history_index], context_element=self.context_element
            )
        elif self._history_index < len(self._history):
            self._history_index = len(self._history)
            self.clear()

    def _can_browse_history(self) -> bool:
        return not self.text or (
            self._history_index < len(self._history)
            and self.input_content == self._history[self._history_index]
        )

    def action_dismiss_command(self) -> None:
        if self.command_context() is not None or self.context_element is not None:
            self.post_message(self.CommandDismissed())
