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

    def do(self, text_area: TextArea, record_selection: bool = True) -> EditResult:
        editor = cast(AttachmentTextArea, text_area)
        result = super().do(editor, record_selection)
        editor.attachment_spans = self.after_spans
        return result

    def undo(self, text_area: TextArea) -> EditResult:
        editor = cast(AttachmentTextArea, text_area)
        result = super().undo(editor)
        editor.attachment_spans = self.before
        return result


class AttachmentTextArea(TuiTextArea):
    """Render names as text spans while keeping attachment identity out of the prompt."""

    attachment_spans: tuple[AttachmentSpan, ...] = ()

    @property
    def plain_document(self) -> Document:
        return cast(Document, self.document)

    @property
    def input_content(self) -> InputContent:
        return self._input_content(self.text, self.attachment_spans)

    @staticmethod
    def _input_content(text: str, spans: tuple[AttachmentSpan, ...]) -> InputContent:
        parts: list[str] = []
        elements: list[InputFile | InputSkillReference] = []
        cursor = offset = 0
        for span in spans:
            part = text[cursor : span.start]
            parts.append(part)
            offset += len(part)
            elements.append(span.attachment.model_copy(update={"text_offset": offset}))
            cursor = span.end
        parts.append(text[cursor:])
        return InputContent.from_elements("".join(parts), tuple(elements))

    def prepare_attachment_insertion(
        self,
    ) -> Callable[[tuple[InputFile, ...]], InputContent]:
        """Capture a selection before an asynchronous import or Session navigation."""
        start, end = sorted(
            self.plain_document.get_index_from_location(location) for location in self.selection
        )
        before = self._input_content(
            self.text[:start], tuple(span for span in self.attachment_spans if span.end <= start)
        )
        after = self._input_content(
            self.text[end:],
            tuple(
                AttachmentSpan(span.start - end, span.end - end, span.attachment)
                for span in self.attachment_spans
                if span.start >= end
            ),
        )

        def insert(items: tuple[InputFile, ...]) -> InputContent:
            return InputContent.from_elements(
                before.text + after.text,
                (
                    *before.elements,
                    *(item.model_copy(update={"text_offset": len(before.text)}) for item in items),
                    *(
                        item.model_copy(update={"text_offset": len(before.text) + item.text_offset})
                        for item in after.elements
                    ),
                ),
            )

        return insert

    def load_input(self, content: InputContent) -> None:
        parts: list[str] = []
        spans: list[AttachmentSpan] = []
        cursor = rendered = 0
        for item in content.elements:
            text = content.text[cursor : item.text_offset]
            label = item.label + " "
            parts.extend((text, label))
            rendered += len(text)
            spans.append(AttachmentSpan(rendered, rendered + len(label), item))
            rendered += len(label)
            cursor = item.text_offset
        parts.append(content.text[cursor:])
        self.attachment_spans = ()
        super().load_text("".join(parts))
        self.attachment_spans = tuple(spans)
        self.move_cursor(self.document.end)

    def load_text(self, text: str) -> None:
        self.attachment_spans = ()
        super().load_text(text)

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        start = self.plain_document.get_index_from_location((line_index, 0))
        end = start + len(line.plain)
        for span in self.attachment_spans:
            if span.start < end and span.end > start:
                line.stylize(theme_for(self).accent, max(0, span.start - start), span.end - start)
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
        previous = self.plain_document.get_index_from_location(self.selection.end)
        for span in self.attachment_spans:
            if span.start < index < span.end:
                edge = span.start if index < previous else span.end
                return Selection.cursor(self.plain_document.get_location_from_index(edge))
        return selection

    def edit(self, edit: Edit) -> EditResult:
        if isinstance(edit, AttachmentEdit):
            return super().edit(edit)
        return super().edit(self._attachment_edit(edit))

    def _attachment_edit(
        self, edit: Edit, inserted: tuple[InputFile | InputSkillReference, ...] = ()
    ) -> AttachmentEdit:
        start = self.plain_document.get_index_from_location(edit.top)
        end = self.plain_document.get_index_from_location(edit.bottom)
        for span in self.attachment_spans:
            if start == end and span.start < start < span.end:
                start = end = span.end
            elif start < span.end and end > span.start:
                start, end = min(start, span.start), max(end, span.end)
        delta = len(edit.text) - (end - start)
        spans: list[AttachmentSpan] = []
        for span in self.attachment_spans:
            if span.end <= start:
                spans.append(span)
            elif span.start >= end:
                spans.append(AttachmentSpan(span.start + delta, span.end + delta, span.attachment))
        offset = start
        for item in inserted:
            spans.append(AttachmentSpan(offset, offset + len(item.label) + 1, item))
            offset += len(item.label) + 1
        result = AttachmentEdit(
            edit.text,
            self.plain_document.get_location_from_index(start),
            self.plain_document.get_location_from_index(end),
            edit.maintain_selection_offset,
        )
        result.before = self.attachment_spans
        result.after_spans = tuple(sorted(spans, key=lambda span: span.start))
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
        edit = Edit("".join(item.label + " " for item in attachments), *self.selection, False)
        result = self.edit(self._attachment_edit(edit, attachments))
        self.history.checkpoint()
        self.move_cursor(result.end_location)


def file_count_label(items: tuple[InputFile, ...], resource_refs: tuple[str, ...] = ()) -> str:
    uploads = sum(item.kind == "upload" for item in items) if items else len(resource_refs)
    references = sum(item.kind == "reference" for item in items)
    return " · ".join(
        f"{count} {label}{'s' if count != 1 else ''}"
        for count, label in ((uploads, "attachment"), (references, "reference"))
        if count
    )
