from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from textual.screen import Screen

from ouro.tui.theme import TuiTheme

if TYPE_CHECKING:
    from ouro.tui.controller import TuiController


class ChooseScreen(Protocol):
    def __call__[T](self, screen: Screen[T]) -> Awaitable[T]: ...


class Notify(Protocol):
    def __call__(
        self,
        message: str,
        *,
        title: str = "",
        severity: Literal["information", "warning", "error"] = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None: ...


class StartWork(Protocol):
    def __call__(
        self,
        work: Callable[[], Coroutine[None, None, None]],
        *,
        group: str,
    ) -> object: ...


@dataclass(frozen=True)
class WorkflowNavigation:
    """Host-owned menu operations; workflows hold no App or controller snapshot."""

    controller: Callable[[], TuiController]
    theme: Callable[[], TuiTheme]
    show: Callable[[Screen], object]
    choose: ChooseScreen
    notify: Notify
    loading: Callable[[str | None], AbstractContextManager[None]]
    start: StartWork

    def notify_error(self, error: Exception | str) -> None:
        self.notify(str(error), severity="error", markup=False)
