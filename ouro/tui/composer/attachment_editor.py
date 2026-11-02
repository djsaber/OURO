"""Atomic attachment spans layered on Textual's native editor and undo history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from rich.text import Text
from textual.document._document import Document, EditResult, Selection
from textual.document._edit import Edit
from textual.widgets import TextArea

from ouro.core.input_content import InputContent, InputFile, InputSkillReference
from ouro.tui.input_presentation import reference_label
from ouro.tui.text_inputs import TuiTextArea
from ouro.tui.theme import theme_for


@dataclass(frozen=True)
class AttachmentSpan:
    start: int
    end: int
    attachment: InputFile | InputSkillReference


class AttachmentEdit(Edit):
    """Let native undo batches own the text and its resource spans together."""

    before: tuple[AttachmentSpan, ...] = ()
    after_spans: tuple[AttachmentSpan, ...] = ()
    cursor_offset: int | None = None
    replaced_selection: str | None = None

    def do(self, text_area: TextArea, record_selection: bool = True) -> EditResult:
        editor = cast(AttachmentTextArea, text_area)
        result = super().do(editor, record_selection)
        editor.attachment_spans = self.after_spans
        if self.cursor_offset is not None:
            self._updated_selection = Selection.cursor(
                editor.plain_document.get_location_from_index(self.cursor_offset)
            )
        return result

    def undo(self, text_area: TextArea) -> EditResult:
        editor = cast(AttachmentTextArea, text_area)
        result = super().undo(editor)
        editor.attachment_spans = self.before
        return result


class AttachmentTextArea(TuiTextArea):
    """Keep editable prose above an atomic reference row, outside the submitted text."""

    attachment_spans: tuple[AttachmentSpan, ...] = ()

    @property
    def plain_document(self) -> Document:
        return cast(Document, self.document)

    @property
    def input_text(self) -> str:
        # The newline before the reference row belongs to the projection, not the prompt.
        text = self.text
        return text[: max(0, self.attachment_spans[0].start - 1)] if self.attachment_spans else text

    @property
    def input_content(self) -> InputContent:
        return _input_with_elements(
            self.input_text, tuple(span.attachment for span in self.attachment_spans)
        )

    def prepare_attachment_insertion(
        self,
    ) -> Callable[[tuple[InputFile, ...]], InputContent]:
        """Capture a selection before an asynchronous import or Session navigation."""
        content = self.input_content
        spans = self.attachment_spans
        start, end = sorted(
            self.plain_document.get_index_from_location(location) for location in self.selection
        )
        text = content.text[:start] + content.text[end:]
        retained = tuple(
            span.attachment for span in spans if not (span.start < end and span.end > start)
        )

        def insert(items: tuple[InputFile, ...]) -> InputContent:
            return _input_with_elements(text, (*retained, *items)) if items else content

        return insert

    def load_input(self, content: InputContent) -> None:
        body = _normalize_newlines(content.text)
        text, spans = _input_document(body, content.elements)
        self.attachment_spans = ()
        super().load_text(text)
        self.attachment_spans = spans
        self.move_cursor(self.plain_document.get_location_from_index(len(body)))

    def load_text(self, text: str) -> None:
        self.attachment_spans = ()
        super().load_text(_normalize_newlines(text))

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        if self.attachment_spans and line_index == self.document.line_count - 1:
            line.stylize(theme_for(self).accent)
        return line

    def _validate_selection(self, selection: Selection) -> Selection:
        selection = super()._validate_selection(selection)
        if not self.attachment_spans:
            return selection
        if not selection.is_empty:
            start = self.plain_document.get_index_from_location(selection.start)
            end = self.plain_document.get_index_from_location(selection.end)
            left, right = sorted((start, end))
            for span in self.attachment_spans:
                if span.start < left < span.end:
                    left = span.start
                if span.start < right < span.end:
                    right = span.end
            positions = (left, right) if start < end else (right, left)
            return Selection(
                *(self.plain_document.get_location_from_index(index) for index in positions)
            )
        index = self.plain_document.get_index_from_location(selection.end)
        for span in self.attachment_spans:
            if span.start < index < span.end:
                edge = span.start if selection.end < self.selection.end else span.end
                return Selection.cursor(self.plain_document.get_location_from_index(edge))
        return selection

    def edit(self, edit: Edit) -> EditResult:
        attachment_edit = edit if isinstance(edit, AttachmentEdit) else self._attachment_edit(edit)
        result = super().edit(attachment_edit)
        # Native rewrap/undo use the real changed range; callers see only their selection.
        return EditResult(
            self.plain_document.get_location_from_index(attachment_edit.cursor_offset)
            if attachment_edit.cursor_offset is not None
            else result.end_location,
            attachment_edit.replaced_selection
            if attachment_edit.replaced_selection is not None
            else result.replaced_text,
        )

    def _attachment_edit(
        self, edit: Edit, inserted: tuple[InputFile | InputSkillReference, ...] = ()
    ) -> AttachmentEdit:
        edit.text = _normalize_newlines(edit.text)
        if not self.attachment_spans and not inserted:
            return AttachmentEdit(
                edit.text, edit.from_location, edit.to_location, edit.maintain_selection_offset
            )
        start = self.plain_document.get_index_from_location(edit.top)
        end = self.plain_document.get_index_from_location(edit.bottom)
        body_length = (
            max(0, self.attachment_spans[0].start - 1)
            if self.attachment_spans
            else len(self.text)
        )
        body_start, body_end = min(start, body_length), min(end, body_length)
        retained = tuple(
            span for span in self.attachment_spans if not (span.start < end and span.end > start)
        )
        delta = len(edit.text) - (body_end - body_start)
        if (
            not inserted
            and len(retained) == len(self.attachment_spans)
            and body_length > 0
            and body_length + delta > 0
        ):
            # Ordinary typing stays a native range edit: only reference offsets move.
            result = AttachmentEdit(
                edit.text,
                self.plain_document.get_location_from_index(body_start),
                self.plain_document.get_location_from_index(body_end),
                edit.maintain_selection_offset,
            )
            result.before = self.attachment_spans
            result.after_spans = tuple(
                AttachmentSpan(span.start + delta, span.end + delta, span.attachment)
                for span in retained
            )
            return result

        # Rebuild the suffix only when references or their separating newline change.
        body = self.text[:body_start] + edit.text + self.text[body_end:body_length]
        projected, spans = _input_document(
            body, (*tuple(span.attachment for span in retained), *inserted)
        )
        result = AttachmentEdit(
            projected[body_start:],
            self.plain_document.get_location_from_index(body_start),
            self.document.end,
            edit.maintain_selection_offset,
        )
        result.before = self.attachment_spans
        result.after_spans = spans
        result.replaced_selection = self.get_text_range(edit.top, edit.bottom)
        if inserted or not edit.maintain_selection_offset:
            result.cursor_offset = (
                body_start + len(edit.text)
                if inserted or edit.text or start <= body_length
                else min(start, len(projected))
            )
        return result

    def insert_attachments(self, attachments: tuple[InputFile, ...]) -> None:
        if not attachments or self.read_only:
            return
        if len(self.input_content.attachments) + len(attachments) > 16:
            raise ValueError("A message can contain at most 16 attachments")
        self._insert_elements(attachments)

    def insert_skills(self, skills: tuple[InputSkillReference, ...]) -> None:
        existing = {item.name for item in self.input_content.skills}
        self._insert_elements(tuple(item for item in skills if item.name not in existing))

    def _insert_elements(self, attachments: tuple[InputFile | InputSkillReference, ...]) -> None:
        if not attachments or self.read_only:
            return
        self.history.checkpoint()
        edit = Edit("", *self.selection, False)
        result = self.edit(self._attachment_edit(edit, attachments))
        self.history.checkpoint()
        self.move_cursor(result.end_location)


def _normalize_newlines(text: str) -> str:
    """Keep span offsets in the editor's LF coordinate system, including Windows pastes."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _input_with_elements(
    text: str, elements: tuple[InputFile | InputSkillReference, ...]
) -> InputContent:
    """References follow the prose; their projected separator is never submitted."""
    return InputContent.from_elements(
        text, tuple(item.model_copy(update={"text_offset": len(text)}) for item in elements)
    )


def _input_document(
    text: str, elements: tuple[InputFile | InputSkillReference, ...]
) -> tuple[str, tuple[AttachmentSpan, ...]]:
    parts = [text]
    if not elements:
        return text, ()
    if text:
        parts.append("\n")
    offset = len(text) + bool(text)
    spans = []
    for item in elements:
        label = reference_label(item.name) + " "
        spans.append(AttachmentSpan(offset, offset + len(label), item))
        parts.append(label)
        offset += len(label)
    return "".join(parts), tuple(spans)


def file_count_label(items: tuple[InputFile, ...], resource_refs: tuple[str, ...] = ()) -> str:
    uploads = sum(item.kind == "upload" for item in items) if items else len(resource_refs)
    references = sum(item.kind == "reference" for item in items)
    return " · ".join(
        f"{count} {label}{'s' if count != 1 else ''}"
        for count, label in ((uploads, "attachment"), (references, "reference"))
        if count
    )
