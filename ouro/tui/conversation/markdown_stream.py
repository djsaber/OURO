"""Incremental Markdown block boundaries and line rendering."""

from __future__ import annotations

from itertools import pairwise

from markdown_it import MarkdownIt

# Rich's Markdown renders through this same parser configuration, so block
# boundaries detected for freezing always agree with the rendered structure.
_MARKDOWN_PARSER = MarkdownIt().enable("strikethrough").enable("table")


_ATOMIC_BLOCKS = frozenset({"fence", "hr", "code_block", "html_block"})


_LIVE_MARKDOWN_BOUNDARY_MAX_CHARS = 8 * 1024


_BACKGROUND_MARKDOWN_CHARS = 2 * 1024


def _block_starts(source: str) -> list[int]:
    """Line indexes where complete top-level markdown blocks begin."""
    return [
        token.map[0]
        for token in _MARKDOWN_PARSER.parse(source)
        if token.level == 0
        and token.map is not None
        and (token.type.endswith("_open") or token.type in _ATOMIC_BLOCKS)
    ]


class AssistantMarkdownStream:
    """Incremental markdown splitting for one streaming answer.

    Completed top-level blocks are returned for immutable rendering as soon as
    a following block starts. The trailing block always stays live: later
    fragments can still extend or re-type it (setext underlines, table
    delimiter rows), so freezing it early could lock in a stale structure.
    """

    def __init__(self) -> None:
        self._source_parts: list[str] = []
        self._lines: list[str] = []
        self._pending_parts: list[str] = []
        self._frozen = 0
        self._tail_chars = 0

    @property
    def source(self) -> str:
        return "".join(self._source_parts)

    @property
    def has_source(self) -> bool:
        return bool(self._source_parts)

    @property
    def tail(self) -> str:
        return self._unfrozen()

    def should_parse_in_background(self, text: str) -> bool:
        """Return whether this append would perform frame-sized boundary parsing."""

        tail_chars = self._tail_chars + len(text)
        return (
            "\n" in text
            and _BACKGROUND_MARKDOWN_CHARS <= tail_chars <= _LIVE_MARKDOWN_BOUNDARY_MAX_CHARS
        )

    @property
    def should_close_in_background(self) -> bool:
        return self._tail_chars >= _BACKGROUND_MARKDOWN_CHARS

    def feed(self, text: str) -> tuple[tuple[str, ...], str]:
        """Accumulate a fragment; return newly completed blocks and the live tail."""
        blocks, _ = self.append(text)
        return blocks, self._unfrozen()

    def append(self, text: str) -> tuple[tuple[str, ...], bool]:
        """Append a fragment without materializing the whole live tail.

        The boolean reports whether completed blocks were removed from the
        live tail, allowing its append-only presentation to reset only at a
        structural Markdown boundary.
        """
        if not text:
            return (), False
        self._source_parts.append(text)
        self._tail_chars += len(text)
        completed_line = self._append_logical_lines(text)
        if not completed_line or self._tail_chars > _LIVE_MARKDOWN_BOUNDARY_MAX_CHARS:
            return (), False

        tail_lines = self._tail_lines()
        starts = _block_starts("".join(tail_lines))
        if len(starts) < 2:
            return (), False
        blocks = tuple("".join(tail_lines[start:end]) for start, end in pairwise(starts))
        frozen_chars = sum(len(line) for line in tail_lines[: starts[-1]])
        self._frozen += starts[-1]
        self._tail_chars -= frozen_chars
        return tuple(block for block in blocks if block.strip()), True

    def close(self) -> tuple[str, ...]:
        """Freeze everything, including the trailing block."""
        tail_lines = self._tail_lines()
        starts = _block_starts("".join(tail_lines))
        boundaries = [*starts, len(tail_lines)]
        blocks = ("".join(tail_lines[start:end]) for start, end in pairwise(boundaries))
        return tuple(block for block in blocks if block.strip())

    def _unfrozen(self) -> str:
        return "".join(self._tail_lines())

    def _tail_lines(self) -> list[str]:
        lines = list(self._lines[self._frozen :])
        if self._pending_parts:
            lines.append("".join(self._pending_parts))
        return lines

    def _append_logical_lines(self, text: str) -> bool:
        """Append source using markdown-it line coordinates in O(new text)."""
        fragments = text.split("\n")
        if len(fragments) == 1:
            self._pending_parts.append(text)
            return False

        first = "".join((*self._pending_parts, fragments[0]))
        self._lines.append(first + "\n")
        self._lines.extend(fragment + "\n" for fragment in fragments[1:-1])
        self._pending_parts = [fragments[-1]] if fragments[-1] else []
        return True


def split_markdown_blocks(text: str) -> tuple[str, ...]:
    """Split a complete markdown document into top-level block slices."""
    stream = AssistantMarkdownStream()
    blocks = list(stream.feed(text)[0])
    blocks.extend(stream.close())
    return tuple(blocks)
