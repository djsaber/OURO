from __future__ import annotations


class RangeSelectionOnly:
    """Preserve pointer range selection while disabling full-widget selection."""

    def text_select_all(self) -> None:
        """Ignore chained-click select-all requests from Textual."""
