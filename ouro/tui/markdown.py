"""Theme-aware assistant Markdown for the OURO TUI."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rich.console import Console, ConsoleOptions, JustifyMethod, RenderResult
from rich.markdown import Heading, MarkdownContext, Paragraph, TableDataElement
from rich.markdown import Markdown as RichMarkdown

from ouro.tui.text_wrapping import ProseText
from ouro.tui.theme import TuiTheme

_LOG = logging.getLogger(__name__)


class _Paragraph(Paragraph):
    def on_enter(self, context: MarkdownContext) -> None:
        super().on_enter(context)
        self.text = ProseText.from_text(self.text)


class _Heading(Heading):
    def on_enter(self, context: MarkdownContext) -> None:
        super().on_enter(context)
        self.text = ProseText.from_text(self.text)


class _TableCell(TableDataElement):
    def __init__(self, justify: JustifyMethod) -> None:
        super().__init__(justify)
        self.content = ProseText.from_text(self.content)


class _ProseMarkdown(RichMarkdown):
    elements = {
        **RichMarkdown.elements,
        "paragraph_open": _Paragraph,
        "heading_open": _Heading,
        "td_open": _TableCell,
        "th_open": _TableCell,
    }


@dataclass(frozen=True, slots=True)
class _AssistantMarkdown:
    source: str
    theme: TuiTheme

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        try:
            markdown = _ProseMarkdown(
                self.source,
                code_theme=self.theme.syntax_theme(),  # type: ignore[arg-type]
                style=self.theme.text_emphasis,
            )
            with console.use_theme(self.theme.rich_theme()):
                yield from console.render(markdown, options)
        except Exception as exc:
            _LOG.warning(
                "Could not render assistant Markdown (%s)",
                type(exc).__name__,
            )
            yield ProseText(self.source, style=self.theme.text_emphasis)


def assistant_markdown(text: str, theme: TuiTheme) -> _AssistantMarkdown:
    """Build the single assistant Markdown projection used by every render path."""

    return _AssistantMarkdown(text, theme)


__all__ = ["assistant_markdown"]
