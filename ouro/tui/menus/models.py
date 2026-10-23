"""Menu choice records and option type aliases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeVar


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    description: str = ""
    accent: str | None = None
    trailing: str = ""
    group: str = ""
    group_trailing: str = ""
    disabled: bool = False
    marker: str = ""
    selection_marker: str = ""
    edit_value: str | None = None
    edit_placeholder: str = ""
    loading: bool = False
    description_accent: str | None = None
    destructive: bool = False
    edit_lines: int = 1
    leading_swatch: str | None = None
    swatches: tuple[str, ...] = ()
    swatch_marker_index: int | None = None
    trailing_accent: str | None = None
    trailing_swatch: str | None = None
    trailing_badge: str = ""


ChoiceSelectionHandler = Callable[[str], Awaitable[tuple[Choice, ...] | None]]


ChoiceRefreshHandler = Callable[[], Awaitable[tuple[Choice, ...] | None]]


ChoiceTabHandler = Callable[[], Awaitable[None]]


ChoiceCancelHandler = Callable[[], bool]


ChoiceAdjustmentHandler = Callable[[str, int], tuple[Choice, ...] | None]


MultiChoiceConfirmationHandler = Callable[
    [tuple[str, ...]],
    Awaitable[tuple[Choice, ...] | None],
]


ChoiceEditHandler = Callable[[str, str], Awaitable[tuple[Choice, ...] | None]]


ScreenResult = TypeVar("ScreenResult")


MenuDestination = Literal["actions", "plugins", "sessions", "tasks", "skills", "help"]
