"""Shared version label typography and horizontal spacing."""

from textual.widgets import Static

from ouro import __version__


class VersionLabel(Static):
    DEFAULT_CSS = """
    VersionLabel {
        width: 100%;
        height: 1;
        padding: 0 2;
        color: #676767;
        text-style: none;
        text-align: right;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(f"v{__version__}", id=id)
