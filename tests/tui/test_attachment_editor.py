from __future__ import annotations

from textual.app import App, ComposeResult
from textual.document._document import Selection

from ouro.core.input_content import InputAttachment, InputContent, InputSkillReference
from ouro.tui.composer.attachment_editor import AttachmentTextArea


class EditorApp(App[None]):
    def compose(self) -> ComposeResult:
        yield AttachmentTextArea()


async def test_attachment_is_one_edit_unit_and_undo_restores_identity() -> None:
    app = EditorApp()
    async with app.run_test() as pilot:
        editor = app.query_one(AttachmentTextArea)
        editor.load_text("before after")
        editor.move_cursor((0, 7))
        editor.insert_attachments((InputAttachment(resource_id="res_image", name="测试图片.jpg"),))
        await pilot.pause()
        original = editor.input_content
        assert original.text == "before after"
        assert original.attachments[0].text_offset == 7
        editor.move_cursor(editor.document.end)
        await pilot.press("backspace")
        assert editor.input_content == InputContent(text="before after")
        editor.undo()
        assert editor.input_content == original
        editor.redo()
        assert editor.input_content.attachments == ()


async def test_partial_replace_removes_whole_attachment_and_preserves_other_spans() -> None:
    app = EditorApp()
    async with app.run_test():
        editor = app.query_one(AttachmentTextArea)
        editor.load_input(
            InputContent(
                text="x y",
                attachments=(
                    InputAttachment(resource_id="one", name="one.txt", text_offset=1),
                    InputAttachment(resource_id="two", name="two.txt", text_offset=3),
                ),
            )
        )
        span = editor.attachment_spans[0]
        editor.replace(
            "replacement",
            editor.plain_document.get_location_from_index(span.start + 1),
            editor.plain_document.get_location_from_index(span.end - 1),
        )
        assert editor.input_content.text == "x yreplacement"
        assert [item.resource_id for item in editor.input_content.attachments] == ["two"]
        assert editor.input_content.attachments[0].text_offset == len("x y")
        editor.undo()
        assert len(editor.input_content.attachments) == 2


async def test_cursor_skips_filename_and_plain_text_does_not_gain_identity() -> None:
    app = EditorApp()
    async with app.run_test() as pilot:
        editor = app.query_one(AttachmentTextArea)
        editor.load_input(
            InputContent(attachments=(InputAttachment(resource_id="r", name="a.txt"),))
        )
        editor.move_cursor((0, 0))
        await pilot.press("right")
        assert editor.cursor_location == (0, 7)
        await pilot.press("left")
        assert editor.cursor_location == (0, 0)
        await pilot.press("delete")
        assert editor.input_content.is_empty
        editor.load_text("a.txt")
        assert editor.input_content == InputContent(text="a.txt")


async def test_prose_edits_rebase_logical_anchors_and_undo_restores_complete_input() -> None:
    app = EditorApp()
    async with app.run_test():
        editor = app.query_one(AttachmentTextArea)
        original = InputContent.from_elements(
            "abcdef",
            (
                InputAttachment(resource_id="one", name="one.txt", text_offset=1),
                InputSkillReference(name="reviewer", text_offset=3),
                InputAttachment(resource_id="two", name="two.txt", text_offset=6),
            ),
        )
        editor.load_input(original)
        assert editor.input_content == original
        # Replacing prose does not delete references whose logical anchors it crosses.
        editor.replace("X", (0, 2), (0, 5))
        edited = editor.input_content
        assert edited.text == "abXf"
        assert [item.text_offset for item in edited.elements] == [1, 3, 4]
        assert [item.name for item in edited.elements] == ["one.txt", "reviewer", "two.txt"]
        editor.undo()
        assert editor.input_content == original
        editor.redo()
        assert editor.input_content == edited
        editor.history.checkpoint()
        editor.replace("", (0, 0), (0, 4))
        assert editor.input_content.text == ""
        assert [item.text_offset for item in editor.input_content.elements] == [0, 0, 0]
        editor.undo()
        assert editor.input_content == edited


async def test_loading_windows_newlines_rebases_anchors_into_editor_coordinates() -> None:
    app = EditorApp()
    async with app.run_test():
        editor = app.query_one(AttachmentTextArea)
        editor.load_input(
            InputContent(
                text="a\r\nb\rc",
                attachments=(InputAttachment(resource_id="r", name="a.txt", text_offset=6),),
                skills=(InputSkillReference(name="reviewer", text_offset=3),),
            )
        )
        content = editor.input_content
        assert content.text == "a\nb\nc"
        assert content.attachments[0].text_offset == 5
        assert content.skills[0].text_offset == 2
        editor.load_input(content)
        assert editor.input_content == content


async def test_delayed_attachment_insertion_rebases_selection_and_retains_identity() -> None:
    app = EditorApp()
    async with app.run_test():
        editor = app.query_one(AttachmentTextArea)
        editor.load_input(
            InputContent(
                text="abcdef",
                attachments=(InputAttachment(resource_id="old", name="old.txt", text_offset=6),),
            )
        )
        editor.selection = Selection((0, 2), (0, 5))
        insert = editor.prepare_attachment_insertion()
        editor.load_text("another session")
        content = insert((InputAttachment(resource_id="new", name="new.txt"),))
        assert content.text == "abf"
        assert [(item.resource_id, item.text_offset) for item in content.attachments] == [
            ("new", 2),
            ("old", 3),
        ]
        assert editor.input_content == InputContent(text="another session")
