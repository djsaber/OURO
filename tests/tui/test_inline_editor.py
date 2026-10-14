from dataclasses import replace

import pytest
from rich.text import Text
from textual import events
from textual.app import App
from textual.widgets import Input, OptionList

from ouro.tui.inline_editor import MultilineEditableChoiceScreen, multiline_edit_prompt
from ouro.tui.keyboard import normalize_windows_modified_enter
from ouro.tui.screens import Choice, EditableChoiceScreen


async def test_empty_single_line_editor_bounds_placeholder_through_typing_and_resize():
    choices = (
        Choice("json", "Settings (JSON)", edit_value="", edit_placeholder="Example: 示例\n" * 40),
    )

    async def save(_field, _value):
        return choices

    app = App()
    async with app.run_test(size=(70, 24)) as pilot:
        form = EditableChoiceScreen(
            title="SETTINGS", placeholder="Filter…", choices=choices, on_save=save
        )
        await app.push_screen(form)
        await pilot.pause()
        await pilot.press("enter")
        options = form.query_one(OptionList)

        async def assert_single_line():
            await pilot.pause()
            form._edit_cursor_visible = True
            form._render_editor()
            prompt = options.get_option("json").prompt
            assert isinstance(prompt, Text)
            assert prompt.cell_len <= form._editor_width()
            assert (
                options._get_visual(options.get_option("json")).get_height(
                    options.styles, form._editor_width()
                )
                == 1
            )
            assert any("reverse" in str(span.style) for span in prompt.spans)

        await assert_single_line()
        await pilot.press("{")
        await assert_single_line()
        await pilot.press("ctrl+u")
        await pilot.resize_terminal(30, 18)
        await assert_single_line()
        assert options.get_option("json").prompt.plain.endswith("…")


async def test_reusable_multiline_form_preserves_newlines_across_save_reopen_and_cancel():
    saved = {}
    choices = (
        Choice("instruction", "Instruction", edit_value="", edit_lines=3),
        Choice("title", "Title", edit_value=""),
    )

    async def save(field, value):
        nonlocal choices
        saved[field] = value
        choices = tuple(
            replace(choice, edit_value=value, trailing=value.split("\n", 1)[0])
            if choice.value == field
            else choice
            for choice in choices
        )
        return choices

    app = App()
    async with app.run_test(size=(80, 24)) as pilot:
        form = MultilineEditableChoiceScreen(
            title="INLINE FORM", placeholder="Filter fields…", choices=choices, on_save=save
        )
        app.push_screen(form)
        await pilot.pause()
        await pilot.press("enter", "a", "shift+enter", "b")
        assert form._edit_text == "a\nb"
        # Exercise the normalized Windows-console event as well as enhanced keys.
        app.post_message(
            normalize_windows_modified_enter(events.Key("enter", "\r"), shift_pressed=True)
        )
        await pilot.pause()
        await pilot.press("c")
        assert form._edit_text == "a\nb\nc"
        options = form.query_one(OptionList)
        prompt = options.get_option("instruction").prompt
        assert prompt.plain.split("\n") == ["a ", "b ", "c "]
        await pilot.press("home", "backspace")
        assert form._edit_text == "a\nbc"
        await pilot.press("shift+enter", "enter")
        assert saved == {"instruction": "a\nb\nc"}
        assert form._editing_value is None
        assert len(app.console.render_lines(options.get_option("instruction").prompt)) == 1
        await pilot.press("enter")
        assert form._edit_text == saved["instruction"]
        form.post_message(events.Paste("\r\nd\ne\nf"))
        await pilot.pause()
        assert form._edit_text == "a\nb\nc\nd\ne\nf"
        assert len(options.get_option("instruction").prompt.plain.split("\n")) == 3
        await pilot.resize_terminal(42, 18)
        await pilot.pause()
        assert "f" in options.get_option("instruction").prompt.plain
        await pilot.press("escape")
        assert saved == {"instruction": "a\nb\nc"}
        form.query_one(Input).value = "Title"
        await pilot.pause()
        await pilot.press("enter")
        form.post_message(events.Paste("one\r\ntwo"))
        await pilot.pause()
        await pilot.press("enter")
        assert saved["title"] == "one two"


@pytest.mark.parametrize(
    "value", ["", "\n\n\n", "first\nsecond\nthird\nfourth", "wide 思考" * 20, "a\t\nb"]
)
def test_multiline_viewport_keeps_three_rows_and_cursor_visible(value):
    first_row = 0
    for width in (20, 9, 40):
        for cursor in (len(value), 0, len(value) // 2):
            prompt, first_row = multiline_edit_prompt(
                value,
                cursor=cursor,
                width=width,
                height=3,
                first_row=first_row,
                cursor_style="reverse",
            )
            lines = prompt.plain.split("\n")
            assert len(lines) == 3
            assert all(Text(line).cell_len <= width for line in lines)
            cursor_spans = [span for span in prompt.spans if "reverse" in str(span.style)]
            assert len(cursor_spans) == 1
            assert cursor_spans[0].end == cursor_spans[0].start + 1
