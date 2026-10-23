from __future__ import annotations

from textual.app import App, ComposeResult

from ouro.core.input_content import InputAttachment, InputContent
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
        assert editor.text == "before @测试图片.jpg after"
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
        editor.replace("replacement", (0, 2), (0, 4))
        assert editor.input_content.text == "xreplacement y"
        assert [item.resource_id for item in editor.input_content.attachments] == ["two"]
        assert editor.input_content.attachments[0].text_offset == len("xreplacement y")
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
