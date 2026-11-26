"""Atomic composer elements layered on Textual's native editor and undo history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from rich.cells import cell_len
from rich.text import Text
from textual.document._document import Document, EditResult, Selection
from textual.document._edit import Edit
from textual.widgets import TextArea

from ouro.core.input_content import InputContent, InputFile, InputSkillReference
from ouro.tui.input_presentation import ContextElement, reference_label
from ouro.tui.text_inputs import TuiTextArea
from ouro.tui.theme import theme_for

type ContentElement = InputFile | InputSkillReference
type ComposerElement = ContentElement | ContextElement


@dataclass(frozen=True)
class ElementSpan:
    start: int
    end: int
    element: ComposerElement


class ElementEdit(Edit):
    """Let native undo batches own the text and its element spans together."""

    before: tuple[ElementSpan, ...] = ()
    after_spans: tuple[ElementSpan, ...] = ()
    cursor_offset: int | None = None
    replaced_selection: str | None = None
    preview_width: int = 0

    def do(self, text_area: TextArea, record_selection: bool = True) -> EditResult:
        editor = cast(ElementTextArea, text_area)
        if not record_selection:
            editor.fit_context_preview(self.preview_width)
        result = super().do(editor, record_selection)
        editor.element_spans = self.after_spans
        if self.cursor_offset is not None:
            self._updated_selection = Selection.cursor(
                editor.plain_document.get_location_from_index(self.cursor_offset)
            )
        return result

    def undo(self, text_area: TextArea) -> EditResult:
        editor = cast(ElementTextArea, text_area)
        editor.fit_context_preview(self.preview_width)
        result = super().undo(editor)
        editor.element_spans = self.before
        return result


class ElementTextArea(TuiTextArea):
    """Keep editable prose above an atomic reference row, outside the submitted text."""

    element_spans: tuple[ElementSpan, ...] = ()

    @property
    def plain_document(self) -> Document:
        return cast(Document, self.document)

    @property
    def input_text(self) -> str:
        text = self.text
        return text[: max(0, self.element_spans[0].start - 1)] if self.element_spans else text

    @property
    def input_content(self) -> InputContent:
        return _input_with_elements(
            self.input_text, tuple(span.element for span in self.element_spans)
        )

    @property
    def context_element(self) -> ContextElement | None:
        if self.element_spans:
            first = self.element_spans[0].element
            if isinstance(first, ContextElement):
                return first
        return None

    def prepare_element_insertion(
        self,
    ) -> Callable[[tuple[ContentElement, ...]], tuple[InputContent, ContextElement | None]]:
        """Capture a selection before an asynchronous import or Session navigation."""
        content = self.input_content
        context = self.context_element
        spans = self.element_spans
        start, end = sorted(
            self.plain_document.get_index_from_location(location) for location in self.selection
        )
        body_start, body_end = min(start, len(content.text)), min(end, len(content.text))
        text = content.text[:body_start] + content.text[body_end:]
        retained = tuple(
            _move_element(span.element, body_start, body_end, 0)
            for span in spans
            if not (span.start < end and span.end > start)
        )

        retained_context = context if context in retained else None

        def insert(items: tuple[ContentElement, ...]) -> tuple[InputContent, ContextElement | None]:
            if not items:
                return content, context
            added = tuple(item.model_copy(update={"text_offset": body_start}) for item in items)
            return _input_with_elements(text, (*retained, *added)), retained_context

        return insert

    def load_input(
        self, content: InputContent, *, context_element: ContextElement | None = None
    ) -> None:
        body = _normalize_newlines(content.text)
        elements = tuple(
            item.model_copy(
                update={"text_offset": len(_normalize_newlines(content.text[: item.text_offset]))}
            )
            for item in content.elements
        )
        text, spans = _input_document(body, elements, context_element, self.wrap_width)
        self.element_spans = spans
        super().load_text(text)
        self.move_cursor(self.plain_document.get_location_from_index(len(body)))

    def on_resize(self) -> None:
        self.fit_context_preview(self.wrap_width)

    def undo(self) -> None:
        super().undo()
        self.fit_context_preview(self.wrap_width)

    def redo(self) -> None:
        super().redo()
        self.fit_context_preview(self.wrap_width)

    def fit_context_preview(self, width: int) -> None:
        """Reflow the display without recording a user edit or discarding native undo."""
        target = self.context_element
        if target is None:
            return
        span = self.element_spans[0]
        label = _element_label(target, width)
        if self.text[span.start : span.end] == label:
            return
        delta = len(label) - (span.end - span.start)
        offsets = [self.plain_document.get_index_from_location(pos) for pos in self.selection]
        document = self.plain_document
        document.replace_range(
            document.get_location_from_index(span.start),
            document.get_location_from_index(span.end),
            label,
        )
        self.element_spans = (
            ElementSpan(span.start, span.end + delta, target),
            *(
                ElementSpan(s.start + delta, s.end + delta, s.element)
                for s in self.element_spans[1:]
            ),
        )
        self._rewrap_and_refresh_virtual_size()
        self.selection = Selection(
            *(
                self.plain_document.get_location_from_index(
                    offset + delta if offset >= span.end else offset
                )
                for offset in offsets
            )
        )

    def load_text(self, text: str) -> None:
        self.element_spans = ()
        super().load_text(_normalize_newlines(text))

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        if self.element_spans and line_index == self.document.line_count - 1:
            line.stylize(theme_for(self).accent)
        return line

    def _validate_selection(self, selection: Selection) -> Selection:
        selection = super()._validate_selection(selection)
        if not self.element_spans:
            return selection
        if not selection.is_empty:
            start = self.plain_document.get_index_from_location(selection.start)
            end = self.plain_document.get_index_from_location(selection.end)
            left, right = sorted((start, end))
            for span in self.element_spans:
                if span.start < left < span.end:
                    left = span.start
                if span.start < right < span.end:
                    right = span.end
            positions = (left, right) if start < end else (right, left)
            return Selection(
                *(self.plain_document.get_location_from_index(index) for index in positions)
            )
        index = self.plain_document.get_index_from_location(selection.end)
        for span in self.element_spans:
            if span.start < index < span.end:
                edge = span.start if selection.end < self.selection.end else span.end
                return Selection.cursor(self.plain_document.get_location_from_index(edge))
        return selection

    def edit(self, edit: Edit) -> EditResult:
        element_edit = edit if isinstance(edit, ElementEdit) else self._element_edit(edit)
        element_edit.preview_width = self.wrap_width
        result = super().edit(element_edit)
        return EditResult(
            self.plain_document.get_location_from_index(element_edit.cursor_offset)
            if element_edit.cursor_offset is not None
            else result.end_location,
            element_edit.replaced_selection
            if element_edit.replaced_selection is not None
            else result.replaced_text,
        )

    def _element_edit(self, edit: Edit, inserted: tuple[ContentElement, ...] = ()) -> ElementEdit:
        edit.text = _normalize_newlines(edit.text)
        if not self.element_spans and not inserted:
            return ElementEdit(
                edit.text, edit.from_location, edit.to_location, edit.maintain_selection_offset
            )
        start = self.plain_document.get_index_from_location(edit.top)
        end = self.plain_document.get_index_from_location(edit.bottom)
        body_length = (
            max(0, self.element_spans[0].start - 1) if self.element_spans else len(self.text)
        )
        body_start, body_end = min(start, body_length), min(end, body_length)
        retained = tuple(
            span for span in self.element_spans if not (span.start < end and span.end > start)
        )
        delta = len(edit.text) - (body_end - body_start)
        if (
            not inserted
            and len(retained) == len(self.element_spans)
            and body_length > 0
            and body_length + delta > 0
        ):
            result = ElementEdit(
                edit.text,
                self.plain_document.get_location_from_index(body_start),
                self.plain_document.get_location_from_index(body_end),
                edit.maintain_selection_offset,
            )
            result.before = self.element_spans
            result.after_spans = tuple(
                ElementSpan(
                    span.start + delta,
                    span.end + delta,
                    _move_element(span.element, body_start, body_end, len(edit.text)),
                )
                for span in retained
            )
            return result

        body = self.text[:body_start] + edit.text + self.text[body_end:body_length]
        content = _input_with_elements(
            body,
            (
                *(
                    _move_element(span.element, body_start, body_end, len(edit.text))
                    for span in retained
                ),
                *(item.model_copy(update={"text_offset": body_start}) for item in inserted),
            ),
        )
        context_element = next(
            (span.element for span in retained if isinstance(span.element, ContextElement)),
            None,
        )
        projected, spans = _input_document(body, content.elements, context_element, self.wrap_width)
        result = ElementEdit(
            projected[body_start:],
            self.plain_document.get_location_from_index(body_start),
            self.document.end,
            edit.maintain_selection_offset,
        )
        result.before = self.element_spans
        result.after_spans = spans
        result.replaced_selection = self.get_text_range(edit.top, edit.bottom)
        if inserted or not edit.maintain_selection_offset:
            result.cursor_offset = (
                body_start + len(edit.text)
                if inserted or edit.text or start <= body_length
                else min(start, len(projected))
            )
        return result

    def insert_elements(self, elements: tuple[ContentElement, ...]) -> None:
        if not elements or self.read_only:
            return
        self.history.checkpoint()
        edit = Edit("", *self.selection, False)
        result = self.edit(self._element_edit(edit, elements))
        self.history.checkpoint()
        self.move_cursor(result.end_location)


def _normalize_newlines(text: str) -> str:
    """Keep span offsets in the editor's LF coordinate system, including Windows pastes."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _input_with_elements(text: str, elements: tuple[ComposerElement, ...]) -> InputContent:
    """Keep logical anchors independent of the reference row's display coordinates."""
    return InputContent.from_elements(
        text,
        tuple(
            sorted(
                (item for item in elements if not isinstance(item, ContextElement)),
                key=lambda item: item.text_offset,
            )
        ),
    )


def _move_element(
    item: ComposerElement,
    start: int,
    end: int,
    inserted_length: int,
) -> ComposerElement:
    """Rebase anchors across prose edits, keeping insertions at an anchor after it."""
    if isinstance(item, ContextElement):
        return item
    offset = item.text_offset
    if offset > start:
        offset = max(start, offset - (end - start)) + inserted_length
    return item.model_copy(update={"text_offset": offset})


def _input_document(
    text: str,
    elements: tuple[ContentElement, ...],
    context_element: ContextElement | None,
    width: int,
) -> tuple[str, tuple[ElementSpan, ...]]:
    parts = [text]
    row: tuple[ComposerElement, ...] = (context_element, *elements) if context_element else elements
    if not row:
        return text, ()
    if text or context_element is not None:
        parts.append("\n")
    offset = sum(map(len, parts))
    spans = []
    for item in row:
        label = _element_label(item, width)
        spans.append(ElementSpan(offset, offset + len(label), item))
        parts.append(label)
        offset += len(label)
    return "".join(parts), tuple(spans)


def _element_label(item: ComposerElement, width: int) -> str:
    if not isinstance(item, ContextElement):
        return reference_label(item.name) + " "
    available = width - cell_len(item.label + "「」 ")
    if available < 1 or not item.preview:
        return item.label + " "
    preview = Text(item.preview)
    preview.truncate(available, overflow="ellipsis")
    return f"{item.label}「{preview.plain}」 "
