from __future__ import annotations

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.document._document import Document
from textual.expand_tabs import expand_text_tabs_from_widths
from textual.geometry import Offset

from ouro.tui.conversation.history_rendering import AssistantTailLines, _MessageContent
from ouro.tui.markdown import assistant_markdown
from ouro.tui.text_inputs import TuiTextArea
from ouro.tui.text_wrapping import ProseText, wrap_offsets, wrap_plain_lines
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.wrapped_document import ProseDocument
from tests.tui.helpers import resize_terminal

_CHINESE = "资质审核结果中触发的规则其实是模块内部的"


@pytest.mark.parametrize("width", [1, 2, 8, 37, 100])
@pytest.mark.parametrize(
    "source",
    [
        "",
        "ascii word " * 30,
        "x" * 301,
        "\n\n",
        "  abc   \n     ",
        "\tEnglish\n 中文\tEnglish 😀\t尾\n",
        "中文测试标点——数字1-234·f(j)🙂 " * 12,
        "Combining e\u0301\u0301 👩‍💻" * 10,
        "a\r\nb\x08c\x1bd",
    ],
)
def test_unstyled_line_preparation_matches_prose_text(source, width):
    expected = ProseText(source).wrap(Console(), width, overflow="fold", no_wrap=False)
    assert wrap_plain_lines(source, width) == [line.plain for line in expected]


def test_user_message_fills_space_after_number_before_wrapping_chinese() -> None:
    console = Console(width=25)
    lines = console.render_lines(_MessageContent("1. " + _CHINESE, DEFAULT_TUI_THEME))
    body = ["".join(segment.text for segment in line) for line in lines]

    # Five cells belong to the frame; 3 + 8 * 2 cells fit in the first content row.
    assert body[1].strip() == "│  1. 资质审核结果中触"


def test_markdown_list_fills_space_after_inline_code_before_wrapping_chinese() -> None:
    console = Console(width=30)
    lines = console.render_lines(assistant_markdown("- `scope` " + _CHINESE, DEFAULT_TUI_THEME))
    body = [text for line in lines if (text := "".join(s.text for s in line).rstrip())]

    assert body[0] == " • scope 资质审核结果中触发的"


class _WrappingApp(App[None]):
    CSS = """
    Screen { overflow: hidden; }
    TuiTextArea {
        width: 100%; height: 8; border: none; padding: 0; scrollbar-size-vertical: 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield TuiTextArea("1. " + _CHINESE, soft_wrap=True)
        yield AssistantTailLines()


async def test_editor_fills_available_cells_without_changing_source() -> None:
    app = _WrappingApp()
    async with app.run_test(size=(24, 16)) as pilot:
        await pilot.pause()
        editor = app.query_one(TuiTextArea)
        assert editor.wrap_width == 23  # Textual reserves one cell for the cursor.
        assert editor.wrapped_document.get_sections(0)[0] == "1. 资质审核结果中触发的"
        assert editor.text == "1. " + _CHINESE


@pytest.mark.parametrize("fragment_size", [1, 7, 100])
async def test_streaming_tail_fills_space_regardless_of_chunk_boundaries(
    fragment_size: int,
) -> None:
    app = _WrappingApp()
    source = "1. " + _CHINESE
    async with app.run_test(size=(24, 16)) as pilot:
        await pilot.pause()
        tail = app.query_one(AssistantTailLines)
        for offset in range(0, len(source), fragment_size):
            tail.append(source[offset : offset + fragment_size])

        assert tail.rendered_text.splitlines()[0] == "1. 资质审核结果中触发的"
        assert tail.text == source


@pytest.mark.parametrize(
    ("source", "width", "expected"),
    [
        ("1. 中文正文", 9, ["1. 中文正", "文"]),
        ("说明 scope 中文正文", 13, ["说明 scope 中", "文正文"]),
        ("中文 hello world", 12, ["中文 hello ", "world"]),
        ("中文（正文）结束", 8, ["中文（正", "文）结束"]),
        ("中文正文，结束", 8, ["中文正", "文，结束"]),
        ("日本語の文章です", 8, ["日本語の", "文章です"]),
        ("한글문장을표시", 8, ["한글문장", "을표시"]),
        (
            "中文 e\u0301e\u0301e\u0301e\u0301e\u0301",
            4,
            ["中文 ", "e\u0301e\u0301e\u0301e\u0301", "e\u0301"],
        ),
        ("中文 👩‍💻👩‍💻👩‍💻", 4, ["中文 ", "👩‍💻👩‍💻", "👩‍💻"]),
        ("中文 abcdefghijk", 6, ["中文 ", "abcdef", "ghijk"]),
        ("中文", 0, ["中文"]),
        ("", 8, [""]),
    ],
)
def test_unicode_wrap_preserves_words_punctuation_and_graphemes(
    source: str, width: int, expected: list[str]
) -> None:
    offsets = wrap_offsets(source, width)
    sections = [part.plain for part in Text(source).divide(offsets)]
    assert sections == expected
    assert "".join(sections) == source


def test_rich_wrapping_preserves_inline_styles_links_and_explicit_newlines() -> None:
    text = ProseText("中文 scope 正文\n\n保留换行", style="italic")
    link = Style(bold=True, link="https://example.com")
    text.stylize(link, 3, 8)
    console = Console(width=13)
    lines = text.wrap(console, 13)

    assert [line.plain for line in lines] == ["中文 scope 正", "文", "", "保留换行"]
    assert lines[0].get_style_at_offset(console, 3) == Style(italic=True) + link
    assert text.plain == "中文 scope 正文\n\n保留换行"


def test_prose_measurement_exposes_chinese_break_opportunities_to_table_layout() -> None:
    console = Console(width=40)
    measurement = ProseText("中文正文").__rich_measure__(console, console.options)
    assert measurement.minimum == 2
    assert measurement.maximum == 8
    mixed = ProseText("中文 scope 正文").__rich_measure__(console, console.options)
    assert mixed.minimum == 5


@pytest.mark.parametrize("width", [7, 8, 13, 24])
@pytest.mark.parametrize("source", ["1. " + _CHINESE, "a甲甲甲甲\t甲乙丙", "甲\t1. 中文正文"])
def test_document_rows_and_cursor_share_tab_and_cell_geometry(source: str, width: int) -> None:
    wrapped = ProseDocument(Document(source), width=width)
    sections = wrapped.get_sections(0)
    assert "".join(sections) == source
    column = tabs = 0
    for y, section in enumerate(sections):
        tab_count = section.count("\t")
        tab_widths = wrapped.get_tab_widths(0)[tabs : tabs + tab_count]
        for index in range(len(section)):
            prefix = expand_text_tabs_from_widths(Text(section[:index]), tab_widths)
            expected = Offset(cell_len(prefix.plain), y)
            assert wrapped.location_to_offset((0, column + index)) == expected
            assert wrapped.offset_to_location(expected) == (0, column + index)
        column += len(section)
        tabs += tab_count


async def test_editor_reflows_edits_undo_redo_resize_and_document_replacement() -> None:
    app = _WrappingApp()
    async with app.run_test(size=(24, 16)) as pilot:
        editor = app.query_one(TuiTextArea)
        editor.focus()
        editor.move_cursor((0, 3))
        editor.insert("新增\n第二行 ")
        await pilot.pause()
        edited = editor.text
        assert edited == "1. 新增\n第二行 " + _CHINESE
        editor.action_undo()
        assert editor.text == "1. " + _CHINESE
        editor.action_redo()
        assert editor.text == edited

        await resize_terminal(pilot, 40, 16)
        expected = ProseDocument(Document(edited), editor.wrap_width)
        assert editor.wrapped_document.lines == expected.lines
        editor.select_all()
        assert editor.selected_text == edited
        editor.load_text("全新文档 " + _CHINESE)
        assert editor.navigator._wrapped_document is editor.wrapped_document
        assert "".join(editor.wrapped_document.get_sections(0)) == editor.text
        editor.soft_wrap = False
        await pilot.pause()
        assert editor.wrapped_document.get_sections(0) == [editor.text]


@pytest.mark.parametrize(
    "source", ["甲乙丙丁，戊己", "中文（正文）结束", "中文 e\u0301e\u0301e\u0301 中文"]
)
async def test_streaming_and_completed_prose_use_the_same_rows(source: str) -> None:
    app = _WrappingApp()
    async with app.run_test(size=(8, 16)) as pilot:
        await pilot.pause()
        tail = app.query_one(AssistantTailLines)
        for character in source:
            tail.append(character)
        expected = ProseText(source).wrap(app.console, tail.content_region.width)
        assert tail.rendered_text.splitlines() == [line.plain.rstrip() for line in expected]


def test_document_edit_rewraps_only_changed_source_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    import ouro.tui.wrapped_document as document_adapter

    document = Document("\n".join([_CHINESE] * 100))
    wrapped = ProseDocument(document, 12)
    observed: list[str] = []
    compute = document_adapter.wrap_offsets

    def record(source: str, width: int) -> list[int]:
        observed.append(source)
        return compute(source, width)

    monkeypatch.setattr(document_adapter, "wrap_offsets", record)
    result = document.replace_range((50, 0), (51, 3), "修改\n增加\n最后")
    wrapped.wrap_range((50, 0), (51, 3), result.end_location)

    assert len(observed) == 3
    assert observed[:2] == ["修改", "增加"]
    assert wrapped.lines == ProseDocument(document, 12).lines


def test_document_reflows_a_reversed_multiline_selection() -> None:
    document = Document("第一行正文\n第二行正文\n第三行正文")
    wrapped = ProseDocument(document, 8)
    result = document.replace_range((2, 2), (0, 2), "新的内容")
    wrapped.wrap_range((2, 2), (0, 2), result.end_location)
    assert wrapped.lines == [["第一新的", "内容行正", "文"]]


def test_native_fast_paths_do_not_segment_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    import ouro.tui.text_wrapping as wrapping

    def reject_segmentation(source: str) -> list[str]:
        raise AssertionError("native and already-fitting paths must not segment text")

    monkeypatch.setattr(wrapping, "line_break_units", reject_segmentation)
    console = Console(width=24)
    assert wrap_offsets("中文", 24) == []
    assert wrap_offsets(_CHINESE, 0) == []
    english = "words " * 100
    assert [line.plain for line in ProseText(english).wrap(console, 24)] == [
        line.plain for line in Text(english).wrap(console, 24)
    ]
    assert ProseDocument(Document(english), 24).get_sections(0)
    assert ProseText(_CHINESE, no_wrap=True).wrap(console, 24)


async def test_streaming_wrap_work_is_bounded_by_new_text_and_pending_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ouro.tui.text_wrapping as wrapping

    app = _WrappingApp()
    fragment = "中文混排 scope "
    lengths: list[int] = []
    segment = wrapping.line_break_units

    def record(source: str) -> list[str]:
        lengths.append(len(source))
        return segment(source)

    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause()
        tail = app.query_one(AssistantTailLines)
        monkeypatch.setattr(wrapping, "line_break_units", record)
        for _ in range(500):
            tail.append(fragment)
        assert lengths
        assert max(lengths) <= tail.content_region.width + len(fragment)
        assert tail.text == fragment * 500
