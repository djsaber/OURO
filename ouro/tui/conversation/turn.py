"""Assistant turn presentation and streaming content assembly."""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget

from ouro.tui.conversation.history_rendering import AssistantTailLines
from ouro.tui.conversation.markdown_stream import AssistantMarkdownStream
from ouro.tui.conversation.reasoning import ReasoningView
from ouro.tui.selection import RangeSelectionOnly


class AssistantTurn(RangeSelectionOnly, Vertical):
    """Live region for one model round: reasoning plus its mutable answer tail.

    Completed top-level blocks freeze into the history buffer as soon as they
    terminate. The mutable tail is shown verbatim through an append-only line
    buffer; this avoids repeatedly parsing and rendering a block whose final
    Markdown structure is not known yet.
    """

    def __init__(
        self,
        *,
        run_id: str | None = None,
        after_tool: bool = False,
        reasoning_expanded: bool = True,
    ) -> None:
        classes = "turn assistant-turn after-tool" if after_tool else "turn assistant-turn"
        super().__init__(classes=classes)
        self.run_id = run_id
        self.reasoning = ReasoningView(expanded=reasoning_expanded)
        self.tail = AssistantTailLines()
        self.tail.display = False
        self._stream = AssistantMarkdownStream()
        self._reasoning_pending = True
        self._tail_reset_pending = False

    def compose(self) -> ComposeResult:
        yield self.reasoning
        yield self.tail

    def reset(
        self,
        *,
        run_id: str | None = None,
        after_tool: bool = False,
        reasoning_expanded: bool = True,
    ) -> None:
        """Replace the live round while retaining the view's mounted surface."""
        if self.is_attached and any(widget.text_selection for widget in self.query(Widget)):
            self.screen.clear_selection()
        self.display = False
        self.run_id = run_id
        self.set_class(after_tool, "after-tool")
        self._stream = AssistantMarkdownStream()
        self._reasoning_pending = True
        self._tail_reset_pending = False
        self.reasoning.reset(expanded=reasoning_expanded)
        self.clear_tail()

    @property
    def source(self) -> str:
        return self._stream.source

    @property
    def has_text(self) -> bool:
        return self._stream.has_source

    @property
    def reasoning_pending(self) -> bool:
        return self._reasoning_pending

    async def feed(self, text: str) -> tuple[str, ...]:
        """Accumulate streamed markdown; return blocks ready to freeze."""
        if not text:
            return ()
        self.reasoning.finish()
        if self._stream.should_parse_in_background(text):
            blocks, tail_reset = await asyncio.to_thread(self._stream.append, text)
        else:
            blocks, tail_reset = self._stream.append(text)
        if tail_reset:
            # Keep the completed source visible while its immutable strips are
            # rendered off-loop. The caller commits the shorter tail immediately
            # after those strips enter history, avoiding a transient blank region.
            self._tail_reset_pending = True
            self.tail.display = True
            self.tail.append(text)
        else:
            self.tail.display = True
            self.tail.append(text)
        return blocks

    def commit_frozen_blocks(self) -> None:
        if not self._tail_reset_pending:
            return
        self._tail_reset_pending = False
        tail = self._stream.tail
        self.tail.display = bool(tail)
        self.tail.replace(tail)

    async def close(self) -> tuple[str, ...]:
        """Freeze the trailing block along with the round."""
        if self._stream.should_close_in_background:
            blocks = await asyncio.to_thread(self._stream.close)
        else:
            blocks = self._stream.close()

        return blocks

    def clear_tail(self) -> None:
        self._tail_reset_pending = False
        self.tail.replace("")
        self.tail.display = False

    def take_reasoning(self) -> tuple[str, float | None]:
        """Detach the live reasoning block for immutable rendering."""
        self._reasoning_pending = False
        self.reasoning.display = False
        return self.reasoning.text, self.reasoning.duration_seconds

    async def append_reasoning(self, text: str, *, started_at: datetime | None = None) -> None:
        await self.reasoning.append(text, started_at=started_at)

    def finish_reasoning(self, duration_seconds: float | None = None) -> None:
        self.reasoning.finish(duration_seconds)

    def set_reasoning_expanded(self, expanded: bool) -> None:
        self.reasoning.set_expanded(expanded)

    @property
    def reasoning_expanded(self) -> bool:
        return self.reasoning.expanded
