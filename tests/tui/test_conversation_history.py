from rich.console import Console

import ouro.tui.conversation_history as conversation_history
from ouro.tui.conversation_history import (
    AssistantMarkdownStream,
    CompactionPresentation,
    _compaction_summary,
    _run_summary,
    split_markdown_blocks,
)
from ouro.tui.state import RunPresentation
from ouro.tui.theme import DEFAULT_TUI_THEME, TUI_DESIGN

_MULTIBLOCK_ANSWER = """Here is the plan:

- step one
- step two

## Details

```python
x = 1
```

| col | val |
| --- | --- |
| a | 1 |

> quoted text

Setext Title
============

Final paragraph.
"""


def _style_at(text, fragment: str):
    return text.get_style_at_offset(Console(), text.plain.index(fragment))


def test_compaction_and_run_summaries_share_visual_structure_and_failure_semantics() -> None:
    compaction = _compaction_summary(
        CompactionPresentation(
            covered_message_count=8,
            tokens_reduced=1_200,
            duration_seconds=3_425.4,
        ),
        DEFAULT_TUI_THEME,
    )
    failed = _run_summary(
        RunPresentation(
            run_id="run_failed",
            status="failed",
            model="deepseek-v4-flash",
            duration_seconds=316.4,
            error="provider returned an empty assistant message",
        ),
        DEFAULT_TUI_THEME,
    )

    assert compaction.plain == (
        "▣ Context compacted • 8 messages summarized • 1.2K tokens reduced • 57m 05.4s"
    )
    assert failed.plain == (
        "▣ deepseek-v4-flash • 5m 16.4s • failed"
        " • provider returned an empty assistant message"
    )
    assert _style_at(compaction, "Context compacted") == _style_at(
        failed, "deepseek-v4-flash"
    )
    assert _style_at(compaction, "Context compacted").bold is True
    assert _style_at(compaction, "Context compacted").color is not None
    assert _style_at(compaction, "Context compacted").color.triplet.hex == (
        TUI_DESIGN.text_secondary
    )
    compaction_separator = compaction.plain.index(TUI_DESIGN.section_separator_mark)
    failed_separator = failed.plain.index(TUI_DESIGN.section_separator_mark)
    assert compaction.get_style_at_offset(Console(), compaction_separator) == (
        failed.get_style_at_offset(Console(), failed_separator)
    )
    assert _style_at(failed, TUI_DESIGN.brand_mark).color is not None
    assert _style_at(failed, TUI_DESIGN.brand_mark).color.triplet.hex == TUI_DESIGN.failure


def test_split_markdown_blocks_preserves_every_block() -> None:
    blocks = split_markdown_blocks(_MULTIBLOCK_ANSWER)
    joined = "".join(blocks)
    required = (
        "Here is the plan",
        "- step one",
        "## Details",
        "x = 1",
        "| col | val |",
        "> quoted text",
        "Setext Title",
        "Final paragraph.",
    )
    assert all(fragment in joined for fragment in required)


def test_split_markdown_blocks_of_blank_document_is_empty() -> None:
    assert split_markdown_blocks("") == ()
    assert split_markdown_blocks("\n\n  \n") == ()


def test_stream_keeps_trailing_block_live_until_next_block_starts() -> None:
    stream = AssistantMarkdownStream()

    blocks, tail = stream.feed("First paragraph")
    assert blocks == ()
    assert tail == "First paragraph"

    blocks, tail = stream.feed(" continues\n\nSecond")
    assert blocks == ("First paragraph continues\n\n",)
    assert tail == "Second"

    blocks, tail = stream.feed(" paragraph\n\nThird")
    assert blocks == ("Second paragraph\n\n",)
    assert tail == "Third"

    assert stream.close() == ("Third",)
    assert stream.source == ("First paragraph continues\n\nSecond paragraph\n\nThird")


def test_stream_does_not_freeze_paragraph_before_setext_underline_arrives() -> None:
    stream = AssistantMarkdownStream()

    # A paragraph followed only by its underline is still one trailing block;
    # freezing it early would lock in the wrong structure.
    blocks, _tail = stream.feed("Title\n")
    assert blocks == ()
    blocks, tail = stream.feed("=====\n")
    assert blocks == ()
    assert tail == "Title\n=====\n"

    blocks, _tail = stream.feed("\nBody text\n")
    assert blocks == ("Title\n=====\n\n",)


def test_stream_accumulates_fragmented_lines_without_rebuilding_source() -> None:
    stream = AssistantMarkdownStream()
    fragments = ("alpha", " beta\n", "\nsecond", " line\n", "third")
    blocks: list[str] = []

    for fragment in fragments:
        completed, _ = stream.append(fragment)
        blocks.extend(completed)

    assert stream.source == "".join(fragments)
    blocks.extend(stream.close())
    assert "".join(blocks) == "".join(fragments)


def test_long_open_block_stops_reparsing_after_bounded_prefix(monkeypatch) -> None:
    stream = AssistantMarkdownStream()
    parse_calls: list[int] = []
    real_block_starts = conversation_history._block_starts

    def counting_block_starts(source: str) -> list[int]:
        parse_calls.append(len(source))
        return real_block_starts(source)

    monkeypatch.setattr(conversation_history, "_block_starts", counting_block_starts)

    stream.append("```text\n")
    for _ in range(64):
        stream.append("x" * 1024 + "\n")

    assert parse_calls
    assert max(parse_calls) <= conversation_history._LIVE_MARKDOWN_BOUNDARY_MAX_CHARS
    calls_before_close = len(parse_calls)
    blocks = stream.close()
    assert len(parse_calls) == calls_before_close + 1
    assert "".join(blocks) == stream.source
